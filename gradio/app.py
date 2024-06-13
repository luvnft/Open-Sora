#!/usr/bin/env python
"""
This script runs a Gradio App for the Open-Sora model.

Usage:
    python demo.py <config-path>
"""

import argparse
import importlib
import os
import subprocess
import sys
import math
import spaces
import torch

import gradio as gr
from tempfile import NamedTemporaryFile
import datetime



MODEL_TYPES = ["v1.2-stage3"]
CONFIG_MAP = {
    "v1.2-stage3": "configs/opensora-v1-2/inference/sample.py",
}
HF_STDIT_MAP = {
    "v1.2-stage3": {
        "ema": "/mnt/jfs/sora_checkpoints/042-STDiT3-XL-2/epoch0-global_step7200/ema.pt",
        "model": "/mnt/jfs/sora_checkpoints/042-STDiT3-XL-2/epoch0-global_step7200/model"
        }
}

# ============================
# Prepare Runtime Environment
# ============================
def install_dependencies(enable_optimization=False):
    """
    Install the required dependencies for the demo if they are not already installed.
    """

    def _is_package_available(name) -> bool:
        try:
            importlib.import_module(name)
            return True
        except (ImportError, ModuleNotFoundError):
            return False

    # flash attention is needed no matter optimization is enabled or not
    # because Hugging Face transformers detects flash_attn is a dependency in STDiT
    # thus, we need to install it no matter what
    if not _is_package_available("flash_attn"):
        subprocess.run(
            f"{sys.executable} -m pip install flash-attn --no-build-isolation",
            env={"FLASH_ATTENTION_SKIP_CUDA_BUILD": "TRUE"},
            shell=True,
        )

    if enable_optimization:
        # install apex for fused layernorm
        if not _is_package_available("apex"):
            subprocess.run(
                f'{sys.executable} -m pip install -v --disable-pip-version-check --no-cache-dir --no-build-isolation --config-settings "--build-option=--cpp_ext" --config-settings "--build-option=--cuda_ext" git+https://github.com/NVIDIA/apex.git',
                shell=True,
            )

        # install ninja
        if not _is_package_available("ninja"):
            subprocess.run(f"{sys.executable} -m pip install ninja", shell=True)

        # install xformers
        if not _is_package_available("xformers"):
            subprocess.run(
                f"{sys.executable} -m pip install -v -U git+https://github.com/facebookresearch/xformers.git@main#egg=xformers",
                shell=True,
            )


# ============================
# Model-related
# ============================
def read_config(config_path):
    """
    Read the configuration file.
    """
    from mmengine.config import Config

    return Config.fromfile(config_path)


def build_models(model_type, config, enable_optimization=False):
    """
    Build the models for the given model type and configuration.
    """
    # build vae
    from opensora.registry import MODELS, build_module

    vae = build_module(config.vae, MODELS).cuda()

    # build text encoder
    text_encoder = build_module(config.text_encoder, MODELS)  # T5 must be fp32
    text_encoder.t5.model = text_encoder.t5.model.cuda()

    # build stdit
    # we load model from HuggingFace directly so that we don't need to
    # handle model download logic in HuggingFace Space
    from opensora.models.stdit.stdit3 import STDiT3, STDiT3Config
    stdit3_config = STDiT3Config.from_pretrained(HF_STDIT_MAP[model_type]['model'])
    stdit = STDiT3(stdit3_config)
    ckpt = torch.load(HF_STDIT_MAP[model_type]['ema'])
    stdit.load_state_dict(ckpt)
    stdit = stdit.cuda()

    # build scheduler
    from opensora.registry import SCHEDULERS

    scheduler = build_module(config.scheduler, SCHEDULERS)

    # hack for classifier-free guidance
    text_encoder.y_embedder = stdit.y_embedder

    # move modelst to device
    vae = vae.to(torch.bfloat16).eval()
    text_encoder.t5.model = text_encoder.t5.model.eval()  # t5 must be in fp32
    stdit = stdit.to(torch.bfloat16).eval()

    # clear cuda
    torch.cuda.empty_cache()
    return vae, text_encoder, stdit, scheduler


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-type",
        default="v1.2-stage3",
        choices=MODEL_TYPES,
        help=f"The type of model to run for the Gradio App, can only be {MODEL_TYPES}",
    )
    parser.add_argument("--output", default="./outputs", type=str, help="The path to the output folder")
    parser.add_argument("--port", default=None, type=int, help="The port to run the Gradio App on.")
    parser.add_argument("--host", default="0.0.0.0", type=str, help="The host to run the Gradio App on.")
    parser.add_argument("--share", action="store_true", help="Whether to share this gradio demo.")
    parser.add_argument(
        "--enable-optimization",
        action="store_true",
        help="Whether to enable optimization such as flash attention and fused layernorm",
    )
    return parser.parse_args()


# ============================
# Main Gradio Script
# ============================
# as `run_inference` needs to be wrapped by `spaces.GPU` and the input can only be the prompt text
# so we can't pass the models to `run_inference` as arguments.
# instead, we need to define them globally so that we can access these models inside `run_inference`

# read config
args = parse_args()
config = read_config(CONFIG_MAP[args.model_type])
torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True

# make outputs dir
os.makedirs(args.output, exist_ok=True)

# disable torch jit as it can cause failure in gradio SDK
# gradio sdk uses torch with cuda 11.3
torch.jit._state.disable()

# set up
install_dependencies(enable_optimization=args.enable_optimization)

# import after installation
from opensora.datasets import IMG_FPS, save_sample
from opensora.utils.misc import to_torch_dtype
from opensora.utils.inference_utils import (
    append_generated,
    apply_mask_strategy,
    collect_references_batch,
    extract_json_from_prompts,
    extract_prompts_loop,
    prepare_multi_resolution_info,
    dframe_to_frame,
    append_score_to_prompts
)
from opensora.models.text_encoder.t5 import text_preprocessing
from opensora.datasets.aspect import get_image_size, get_num_frames

# some global variables
dtype = to_torch_dtype(config.dtype)
device = torch.device("cuda")

# build model
vae, text_encoder, stdit, scheduler = build_models(args.model_type, config, enable_optimization=args.enable_optimization)


def run_inference(mode, prompt_text, resolution, aspect_ratio, length, motion_strength, aesthetic_score, use_motion_strength, use_aesthetic_score, use_timestep_transform, reference_image, seed, sampling_steps, cfg_scale):
    torch.manual_seed(seed)
    with torch.inference_mode():
        # ======================
        # 1. Preparation arguments
        # ======================
        # parse the inputs
        # frame_interval must be 1 so  we ignore it here
        image_size = get_image_size(resolution, aspect_ratio)
        condition_frame_length = config.condition_frame_length
        
        # compute generation parameters
        if mode == "Text2Image":
            num_frames = 1
            num_loop = 1
            fps = IMG_FPS
        else:
            fps = config.fps
            num_frames = config.num_frames
            seconds = int(length.rstrip('s'))
            
            if seconds <= 16:
                num_frames = get_num_frames(length)
                num_loop = 1
            else:
                total_num_frames = fps * seconds
                condition_real_frame_length = dframe_to_frame(condition_frame_length)
                num_subsequence_loop = int((total_num_frames - num_frames) / (num_frames - condition_real_frame_length))
                num_loop = num_subsequence_loop + 1
        
        input_size = (num_frames, *image_size)
        latent_size = vae.get_latent_size(input_size)
        multi_resolution = "OpenSora"
        align = 5
        
        # prepare reference
        if mode == "Text2Image":
            mask_strategy = [None]
        elif mode == "Text2Video":
            if reference_image is not None:
                mask_strategy = ['0']
            else:
                mask_strategy = [None]
        else:
            raise ValueError(f"Invalid mode: {mode}")
        
        # prepare refs
        if mode == "Text2Image":
            refs = [""]
        elif mode == "Text2Video":
            if reference_image is not None:
                # save image to disk
                from PIL import Image
                im = Image.fromarray(reference_image)
                temp_file = NamedTemporaryFile(suffix=".png")
                im.save(temp_file.name)
                refs = [temp_file.name]
            else:
                refs = [""]
        else:
            raise ValueError(f"Invalid mode: {mode}")
        
        # process prompt
        batch_prompts = [prompt_text]
        batch_prompts, refs, mask_strategy = extract_json_from_prompts(batch_prompts, refs, mask_strategy)
        
        refs = collect_references_batch(refs, vae, image_size)
        
        # process scores
        use_motion_strength = use_motion_strength and mode != "Text2Image"
        batch_prompts = append_score_to_prompts(
            batch_prompts,
            aes=aesthetic_score if use_aesthetic_score else None,
            flow=motion_strength if use_motion_strength else None
            )
        
        # multi-resolution info 
        model_args = prepare_multi_resolution_info(
            multi_resolution, len(batch_prompts), image_size, num_frames, fps, device, dtype
        )

        # =========================
        # Generate image/video
        # =========================
        video_clips = []
        
        for loop_i in range(num_loop):
            # 4.4 sample in hidden space
            batch_prompts_loop = extract_prompts_loop(batch_prompts, loop_i)
            batch_prompts_cleaned = [text_preprocessing(prompt) for prompt in batch_prompts_loop]
            
            # == loop ==
            if loop_i > 0:
                refs, mask_strategy = append_generated(vae, video_clips[-1], refs, mask_strategy, loop_i, condition_frame_length)
            
            # == sampling ==
            z = torch.randn(len(batch_prompts), vae.out_channels, *latent_size, device=device, dtype=dtype)
            masks = apply_mask_strategy(z, refs, mask_strategy, loop_i, align=align)
            
            # 4.6. diffusion sampling
            # hack to update num_sampling_steps and cfg_scale
            scheduler_kwargs = config.scheduler.copy()
            scheduler_kwargs.pop('type')
            scheduler_kwargs['num_sampling_steps'] = sampling_steps
            scheduler_kwargs['cfg_scale'] = cfg_scale
            scheduler_kwargs['use_timestep_transform'] = use_timestep_transform

            scheduler.__init__(
                **scheduler_kwargs
            )
            samples = scheduler.sample(
                stdit,
                text_encoder,
                z=z,
                prompts=batch_prompts_cleaned,
                device=device,
                additional_args=model_args,
                progress=True,
                mask=masks,
            )
            samples = vae.decode(samples.to(dtype), num_frames=num_frames)
            video_clips.append(samples)
            
        # =========================
        # Save output
        # =========================
        video_clips = [val[0] for val in video_clips]
        for i in range(1, num_loop):
            video_clips[i] = video_clips[i][:, dframe_to_frame(condition_frame_length) :]
        video = torch.cat(video_clips, dim=1)
        current_datetime = datetime.datetime.now()
        timestamp = current_datetime.timestamp()
        save_path = os.path.join(args.output, f"output_{timestamp}")
        saved_path = save_sample(video, save_path=save_path, fps=fps)
        torch.cuda.empty_cache()
        return saved_path

@spaces.GPU(duration=200)
def run_image_inference(
    prompt_text, 
    resolution, 
    aspect_ratio, 
    length, 
    motion_strength, 
    aesthetic_score, 
    use_motion_strength, 
    use_aesthetic_score,
    use_timestep_transform,
    reference_image,
    seed,
    sampling_steps,
    cfg_scale):
    return run_inference(
        "Text2Image", 
        prompt_text, 
        resolution,
        aspect_ratio,
        length,
        motion_strength,
        aesthetic_score,
        use_motion_strength,
        use_aesthetic_score,
        use_timestep_transform,
        reference_image,
        seed,
        sampling_steps,
        cfg_scale)

@spaces.GPU(duration=200)
def run_video_inference(
    prompt_text,
    resolution,
    aspect_ratio,
    length,
    motion_strength,
    aesthetic_score,
    use_motion_strength,
    use_aesthetic_score, 
    use_timestep_transform,
    reference_image, 
    seed,
    sampling_steps,
    cfg_scale):
    if (resolution == "480p" and length == "16s") or \
        (resolution == "720p" and length in ["8s", "16s"]):
        gr.Warning("Generation is interrupted as the combination of 480p and 16s will lead to CUDA out of memory")
    else:
        return run_inference(
            "Text2Video",
            prompt_text, 
            resolution,
            aspect_ratio, 
            length, 
            motion_strength, 
            aesthetic_score, 
            use_motion_strength,
            use_aesthetic_score, 
            use_timestep_transform,
            reference_image, 
            seed,
            sampling_steps, 
            cfg_scale
            )


def main():
    # create demo
    with gr.Blocks() as demo:
        with gr.Row():
            with gr.Column():
                gr.HTML(
                    """
                <div style='text-align: center;'>
                    <p align="center">
                        <img src="https://github.com/hpcaitech/Open-Sora/raw/main/assets/readme/icon.png" width="250"/>
                    </p>
                    <div style="display: flex; gap: 10px; justify-content: center;">
                        <a href="https://github.com/hpcaitech/Open-Sora/stargazers"><img src="https://img.shields.io/github/stars/hpcaitech/Open-Sora?style=social"></a>
                        <a href="https://hpcaitech.github.io/Open-Sora/"><img src="https://img.shields.io/badge/Gallery-View-orange?logo=&amp"></a>
                        <a href="https://discord.gg/kZakZzrSUT"><img src="https://img.shields.io/badge/Discord-join-blueviolet?logo=discord&amp"></a>
                        <a href="https://join.slack.com/t/colossalaiworkspace/shared_invite/zt-247ipg9fk-KRRYmUl~u2ll2637WRURVA"><img src="https://img.shields.io/badge/Slack-ColossalAI-blueviolet?logo=slack&amp"></a>
                        <a href="https://twitter.com/yangyou1991/status/1769411544083996787?s=61&t=jT0Dsx2d-MS5vS9rNM5e5g"><img src="https://img.shields.io/badge/Twitter-Discuss-blue?logo=twitter&amp"></a>
                        <a href="https://raw.githubusercontent.com/hpcaitech/public_assets/main/colossalai/img/WeChat.png"><img src="https://img.shields.io/badge/微信-小助手加群-green?logo=wechat&amp"></a>
                        <a href="https://hpc-ai.com/blog/open-sora-v1.0"><img src="https://img.shields.io/badge/Open_Sora-Blog-blue"></a>
                    </div>
                    <h1 style='margin-top: 5px;'>Open-Sora: Democratizing Efficient Video Production for All</h1>
                </div>
                """
                )

        with gr.Row():
            with gr.Column():
                prompt_text = gr.Textbox(
                    label="Prompt",
                    placeholder="Describe your video here",
                    lines=4,
                )
                resolution = gr.Radio(
                     choices=["144p", "240p", "360p", "480p", "720p"],
                     value="480p",
                    label="Resolution", 
                )
                aspect_ratio = gr.Radio(
                     choices=["9:16", "16:9", "3:4", "4:3", "1:1"],
                     value="9:16",
                    label="Aspect Ratio (H:W)", 
                )
                length = gr.Radio(
                    choices=["2s", "4s", "8s", "16s"], 
                    value="2s",
                    label="Video Length", 
                    info="only effective for video generation, 8s may fail as Hugging Face ZeroGPU has the limitation of max 200 seconds inference time."
                )

                with gr.Row():
                    seed = gr.Slider(
                        value=1024,
                        minimum=1,
                        maximum=2048,
                        step=1,
                        label="Seed"
                    )

                    sampling_steps = gr.Slider(
                        value=30,
                        minimum=1,
                        maximum=200,
                        step=1,
                        label="Sampling steps"
                    )
                    cfg_scale = gr.Slider(
                        value=7.0,
                        minimum=0.0,
                        maximum=10.0,
                        step=0.1,
                        label="CFG Scale"
                    )
                    
                with gr.Row():
                    with gr.Column():
                        motion_strength = gr.Slider(
                            value=100,
                            minimum=0,
                            maximum=500,
                            step=1,
                            label="Motion Strength",
                            info="only effective for video generation"
                        )
                        use_motion_strength = gr.Checkbox(value=False, label="Enable")
                        
                    with gr.Column():
                        aesthetic_score = gr.Slider(
                            value=6,
                            minimum=4,
                            maximum=7,
                            step=1,
                            label="Aesthetic",
                            info="effective for text & video generation"
                        )
                        use_aesthetic_score = gr.Checkbox(value=True, label="Enable")
                        
                use_timestep_transform = gr.Checkbox(value=True, label="Use Time Transform")
                        
                
                reference_image = gr.Image(
                    label="Reference Image (Optional)",
                    show_download_button=True
                )
            
            with gr.Column():
                output_video = gr.Video(
                    label="Output Video",
                    height="100%"
                )

        with gr.Row():
             image_gen_button = gr.Button("Generate image")
             video_gen_button = gr.Button("Generate video")
        

        image_gen_button.click(
             fn=run_image_inference, 
             inputs=[prompt_text, resolution, aspect_ratio, length, motion_strength, aesthetic_score, use_motion_strength, use_aesthetic_score, use_timestep_transform, reference_image, seed, sampling_steps, cfg_scale], 
             outputs=reference_image
             )
        video_gen_button.click(
             fn=run_video_inference, 
             inputs=[prompt_text, resolution, aspect_ratio, length, motion_strength, aesthetic_score, use_motion_strength, use_aesthetic_score, use_timestep_transform, reference_image, seed, sampling_steps, cfg_scale], 
             outputs=output_video
             )

    # launch
    demo.launch(server_port=args.port, server_name=args.host, share=args.share)


if __name__ == "__main__":
    main()
