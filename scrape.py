from wrapt_timeout_decorator import *
from diffusers import AutoencoderKL
from transformers import CLIPTokenizer, CLIPTextModel
from io import BytesIO
from PIL import Image, ImageOps
from torchvision.transforms.functional import pil_to_tensor, to_pil_image
from einops import rearrange
from huggingface_hub import HfApi
from im2im.unet import UNet
import logging
import os
import time
import shutil
import json
import httphandler
import torch
import traceback
import torch.multiprocessing as mp
import torch.multiprocessing.queue
import tqdm
import cv2
import math
import numpy as np

#I don't remember what I was gonna use this for lol
class ImpQueue(torch.multiprocessing.queue.Queue):
    def to_list(self):
        with self.mutex:
            return list(self.queue)
        
mp.set_start_method('spawn', force=True)
mp.freeze_support()

GPUS = 3
BATCH_SIZE = 10
UPLOAD_BATCH_SIZE = 10

OUTPUT_DIR = "shutter"
JSON_PATH = "video_list.json"
MODEL = "runwayml/stable-diffusion-v1-5"

DELAY = 5
MAX_RETRIES = 3
TIMEOUT_LEN = 30

HF_DATASET_PATH = "chavinlo/tempofunk"
HF_DATASET_BRANCH = "testing-5"
MAX_FRAMES = 240

MAX_IN_PROCESS_VIDEOS = 100
MAX_IN_UPLOAD_VIDEOS = 100

RESIZE_VIDEO = True
REMOVE_WATERMARK = True

#Debugging
TESTING_MODE = False # DISABLE THIS

ENABLE_DEBUG = True

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(f"{OUTPUT_DIR}/error", exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger()
logger.addHandler(logging.FileHandler('logfile.log', 'a'))

model_list = []

dlib = httphandler.HTTPHandler()
video_list = json.load(open(JSON_PATH, "r"))
tokenizer = CLIPTokenizer.from_pretrained(MODEL, subfolder="tokenizer")

init_time = time.time()

"""

text; video; (Scrapper) -> process them (GPU engines) -> (Scrapper) upload to HF
Scraper stops if Queue shape is more than 100 videos in the queue

"""

def load_model(path: str, device: str | torch.device = 'cuda') -> UNet:
    with open(os.path.join(path, 'configuration.json'), 'r') as f:
        config = json.load(f)
    model = UNet(**config['config'])
    model.load_state_dict(torch.load(os.path.join(path, config['ckpt'])))
    model = model.eval().requires_grad_(False).to(device = device, memory_format = torch.contiguous_format)
    model = torch.compile(model, mode = 'max-autotune', fullgraph = True)
    return model

def upload_thread(uplo_queue: ImpQueue, id_list, failed_id):

    api = HfApi()
    processing_chunk = []

    while True:
        if uplo_queue.qsize() >= UPLOAD_BATCH_SIZE:
            try:
                processing_chunk.clear()
                for i in range(UPLOAD_BATCH_SIZE):
                    processing_chunk.append(uplo_queue.get())

                print("Uploading batch of videos...")
                
                for batch in processing_chunk:
                    batch: dict
                    batch_id = batch['metadata']['id']

                    #batch is a video contents. aka metadata, video, prompt

                    for key, value in batch.items():
                        if key == 'metadata':
                            path_to_upload = f"data/metadata/{batch_id}.json"
                            obj_to_upload = BytesIO(json.dumps(value).encode())
                        elif key == 'video':
                            path_to_upload = f"data/videos/{batch_id}.npy"
                            obj_to_upload = value
                        elif key == 'prompt':
                            path_to_upload = f"data/prompts/{batch_id}.npy"
                            obj_to_upload = value

                        api.upload_file(
                            repo_id=HF_DATASET_PATH,
                            repo_type="dataset",
                            path_or_fileobj=obj_to_upload,
                            path_in_repo=path_to_upload,
                            revision=HF_DATASET_BRANCH,
                        )

                        print(f"Uploaded {key} for video {batch_id}")

                _tmp_list = []

                for i in id_list:
                    _tmp_list.append(i)

                _failed_list = []

                for i in failed_id:
                    _failed_list.append(i)

                json_bytes = BytesIO(json.dumps(_tmp_list).encode())
                failed_json_bytes = BytesIO(json.dumps(_failed_list).encode())

                api.upload_file(
                    repo_id=HF_DATASET_PATH,
                    repo_type="dataset",
                    path_or_fileobj=json_bytes,
                    path_in_repo="data/id_list.json",
                    revision=HF_DATASET_BRANCH,
                )

                api.upload_file(
                    repo_id=HF_DATASET_PATH,
                    repo_type="dataset",
                    path_or_fileobj=failed_json_bytes,
                    path_in_repo="data/failed_id_list.json",
                    revision=HF_DATASET_BRANCH,
                )

                print("Uploaded id list")
            except Exception as e:
                print("Error uploading videos to HF, skipping batch...")
                print(e)
                traceback.print_exc()
                time.sleep(1)
        else:
            print("Waiting for videos to upload..., current queue size: ", uplo_queue.qsize())
            time.sleep(1)

def processing_thread(proc_queue: ImpQueue, uplo_queue: ImpQueue, gpu_id: int, id_list: list, failed_id_list: list):
    video_vae_frames = []

    print("Loading models on GPU ", gpu_id)

    vae: AutoencoderKL = AutoencoderKL.from_pretrained(MODEL, subfolder='vae').to(f'cuda:{gpu_id}')
    text_encoder: CLIPTextModel = CLIPTextModel.from_pretrained(MODEL, subfolder='text_encoder').to(f'cuda:{gpu_id}')
    watermark_remover = load_model("/home/redmond/tempofunk-scrapper/im2im-sswm", device=f'cuda:{gpu_id}')

    print("Finished loading models on GPU ", gpu_id)

    while True:
        if uplo_queue.qsize() >= MAX_IN_UPLOAD_VIDEOS:
            print("Max in memory videos reached.")
            time.sleep(1)
            continue

        try:
            data = proc_queue.get(timeout=1)
        except Exception:
            #TODO: no. this is wrong. There was an exception like queue.empty() or something before
            #but coulndn't find it on multiprocessing from torch
            continue

        metadata: dict = data['metadata'] # <- metadata as dict
        video: str = data['video'] # <- path to video (/tmp/)
        prompt: str = data['prompt'] # <- prompt as string

        print("Processing video", metadata['id'], "with prompt", prompt)

        try:

            cap = cv2.VideoCapture(video)
            _tqdm_bar = tqdm.tqdm(total=MAX_FRAMES, position=gpu_id, leave=False)

            video_vae_frames.clear()
            
            # _vid_id = metadata['id']
            # _tmp_dir = f"{OUTPUT_DIR}/{_vid_id}"
            # os.makedirs(_tmp_dir, exist_ok=True)

            for i in range(MAX_FRAMES):
                success, image = cap.read()
                image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                # image is a numpy array
                im_pil = Image.fromarray(image, mode='RGB')
                #im_pil.save(f"{_tmp_dir}/{i}_before.png")

                #_conv_im = torch.tensor(np.asarray(im_pil)).permute(0,3,1,2)
                if REMOVE_WATERMARK is True:

                    # Stack it so that the dewatermarker can process it (im too lazy to change the code lol)
                    _conv_im = torch.stack([torch.tensor(np.asarray(im_pil))]).permute(0,3,1,2)
                    _conv_im: torch.Tensor = _conv_im.to(torch.float32).div(255).to(memory_format = torch.contiguous_format)

                    # Dewatermarker start
                    _conv_im = _conv_im.to(f'cuda:{gpu_id}', non_blocking = True)
                    bs,_,h,w = _conv_im.shape
                    right = math.ceil(w / watermark_remover.ksize) * watermark_remover.ksize - w
                    bottom = math.ceil(h / watermark_remover.ksize) * watermark_remover.ksize - h
                    x = torch.nn.functional.pad(_conv_im, [0, right, 0, bottom], mode = 'reflect')
                    # print("x.shape", x.shape)
                    # print("x.device", x.device)
                    
                    with torch.autocast(torch.device(f'cuda:{gpu_id}').type):
                        y = watermark_remover(x)
                    y = y[:,:,0:h,0:w]
                    y = y.mul(255).round().clamp(0,255).permute(0,2,3,1).to(device = 'cpu', dtype = torch.uint8).numpy()
                    im_pil = Image.fromarray(y[0])
                    #im_pil.save(f"{_tmp_dir}/{i}_after.png")
                    # Dewatermarker end

                if RESIZE_VIDEO is True:
                    im_pil = ImageOps.fit(im_pil, (512, 512), centering = (0.5, 0.5))
                #im_pil.save(f"{_tmp_dir}/{i}_resize.png")
                with torch.inference_mode():
                    m = pil_to_torch(im_pil, f'cuda:{gpu_id}').unsqueeze(0)
                    m = vae.encode(m).latent_dist
                    video_vae_frames.append(m.mean.squeeze().cpu().numpy())
                _tqdm_bar.update(1)

            print("Video", metadata['id'], "processed.")

            cap.release()
            os.remove(video)
            _tqdm_bar.close()

            # Encode prompt
            tokenized_prompt = tokenizer(
                    [ prompt ],
                    return_tensors="pt",
                    truncation=True,
                    return_overflowing_tokens=True,
                    padding="max_length",
                ).input_ids.to(f'cuda:{gpu_id}')
            encoded_prompt = text_encoder(input_ids=tokenized_prompt)
            #print("La cosa:", encoded_prompt)
            #encoded_prompt = encoded_prompt.to(f'cuda:{gpu_id}').last_hidden_state

            if TESTING_MODE is True:
                _tmp_embed = torch.stack(video_vae_frames)
                _tmp_embed = rearrange(_tmp_embed, 'f c h w -> c f h w')
                torch.save(_tmp_embed, "tmp_embed.pt")
                print("Saved Embed!")
                exit()

            # Save them
            video_bytes = BytesIO()
            prompt_bytes = BytesIO()

            np_video_vae_frames = np.array(video_vae_frames)
            np_encoded_prompt = encoded_prompt.last_hidden_state.cpu().detach().numpy()

            np.save(video_bytes, np_video_vae_frames)
            np.save(prompt_bytes, np_encoded_prompt)

            # torch.save(video_vae_frames, video_bytes)
            # torch.save(encoded_prompt, prompt_bytes)

            video_bytes.seek(0)
            prompt_bytes.seek(0)

            # Put on the Upload Queue
            uplo_queue.put({
                'metadata': metadata,
                'video': video_bytes,
                'prompt': prompt_bytes
            })
            id_list.append(metadata['id'])
        except Exception as e:
            print("ERROR - Video ", metadata['id'], "with prompt", prompt, ":", e)
            failed_id_list.append(metadata['id'])
            cap.release()
            # filename, ext = os.path.splitext(video)
            # shutil.copyfile(video, f"{OUTPUT_DIR}/error/{metadata['id']}.{ext}")
            # os.remove(video)
            # print("Saved faulty video to: ", f"{OUTPUT_DIR}/error/{metadata['id']}.{ext}")
            _tqdm_bar.close()
            continue

def main():
    api = HfApi()
    manager = mp.Manager()

    proc_queue = ImpQueue(ctx=mp.get_context('spawn'))
    uplo_queue = ImpQueue(ctx=mp.get_context('spawn'))

    id_list = manager.list()
    failed_id_list = manager.list()

    for gpu_id in range(0, GPUS):
        mp.Process(target=processing_thread, args=(proc_queue, uplo_queue, gpu_id, id_list, failed_id_list,)).start()

    time.sleep(10)

    mp.Process(target=upload_thread, args=(uplo_queue, id_list, failed_id_list,)).start()

    api.create_branch(repo_id=HF_DATASET_PATH, branch=HF_DATASET_BRANCH, exist_ok=True, repo_type="dataset")
    
    thread_list = []
    list_index = 0
    
    while True:
        #display in HH:MM:SS
        print("Total videos scraped:", list_index, "in", time.strftime('%H:%M:%S', time.gmtime(time.time() - init_time)))
        print("Total Failed:", len(failed_id_list))
        print("Queue Slots:", proc_queue.qsize(), "/", MAX_IN_PROCESS_VIDEOS)
        if proc_queue.qsize() >= MAX_IN_PROCESS_VIDEOS:
            print("Max in process videos reached, waiting...")
            time.sleep(1)
            continue
        else:
            thread_list = [t for t in thread_list if t.is_alive()]
            cur_batch = BATCH_SIZE - len(thread_list)
            for i in range(0, cur_batch):
                thread_list.append(mp.Process(target=scrape_post_timeout, args=(video_list[list_index + i], proc_queue,)))
            for thread in thread_list:
                if not thread.is_alive():
                    try:
                        thread.start()
                    except Exception as e:
                        traceback.print_exc()
                        print(e)
            list_index += cur_batch
            time.sleep(DELAY)

            #print thread status
            for thread in thread_list:
                print(thread.is_alive())

def pil_to_torch(image, device = 'cpu'):
    return (2 * (pil_to_tensor(image).to(dtype=torch.float32, device=device)) / 255) - 1

def scrape_post_timeout(videometa, proc_queue: ImpQueue, retry_n=0):
    try:
        scrape_post(videometa, proc_queue)
    except Exception as e:
        traceback.print_exc()
        if retry_n < MAX_RETRIES:
            scrape_post_timeout(videometa, proc_queue, retry_n=retry_n + 1)
        else:
            id = videometa['id']
            logger.info(f'Video with ID {id} failed after {MAX_RETRIES} tries')

@timeout(TIMEOUT_LEN)
def get_post_wrapper(videometa):
    return dlib.get_post(videometa)

def scrape_post(videometa, proc_queue: ImpQueue):
    try:
        id = videometa['id']
        stream, ext = get_post_wrapper(videometa)
        _output_path = "/tmp/" + id + "." + ext
        shutil.copyfileobj(stream, open(_output_path, 'wb'))
        proc_queue.put({
            "metadata": videometa,
            "video": _output_path,
            "prompt": videometa['description']
        })
        
    except Exception as e:
        logger.info(e)
        traceback.print_exc()
        raise Exception("General Error")

if __name__ == "__main__":
    main()