"""One bounded local-video node; VM address and model paths cannot enter the graph."""

from pathlib import Path

import folder_paths
from comfy_api.input_impl import VideoFromFile

from .sdcpp_client import generate, make_request


# Структура MaxWanVM109 объединяет связанные данные и операции модуля.
class MaxWanVM109:
    RETURN_TYPES = ("VIDEO",)
    FUNCTION = "generate"
    CATEGORY = "MAX/video"

    # Выполняем операцию INPUT_TYPES в рамках данного модуля.
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "text": ("STRING", {"multiline": True}),
            "negative": ("STRING", {"forceInput": True}),
            "seed": ("INT", {"default": 42, "min": 0, "max": 2**63 - 1}),
            "frames": ("INT", {"default": 9, "min": 1, "max": 33}),
            "width": ("INT", {"default": 192, "min": 128, "max": 256, "step": 16}),
            "height": ("INT", {"default": 192, "min": 128, "max": 256, "step": 16}),
            "steps": ("INT", {"default": 8, "min": 1, "max": 30}),
            "fps": ("INT", {"default": 8, "min": 8, "max": 8}),
        }}

    # Выполняем операцию generate в рамках данного модуля.
    def generate(self, text, negative, seed, frames, width, height, steps, fps):
        request = make_request(text, negative, seed, frames, width, height, steps, fps)
        directory = Path(folder_paths.get_temp_directory()) / "max-vm109"
        return (VideoFromFile(str(generate(request, directory))),)


# Структура MaxWanNegative объединяет связанные данные и операции модуля.
class MaxWanNegative:
    RETURN_TYPES = ("STRING",)
    FUNCTION = "value"
    CATEGORY = "MAX/video"

    # Выполняем операцию INPUT_TYPES в рамках данного модуля.
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"text": ("STRING", {"multiline": True})}}

    # Выполняем операцию value в рамках данного модуля.
    def value(self, text):
        return (text,)


NODE_CLASS_MAPPINGS = {"MaxWanVM109": MaxWanVM109, "MaxWanNegative": MaxWanNegative}
NODE_DISPLAY_NAME_MAPPINGS = {"MaxWanVM109": "Wan 2.1 — VM 109 Vulkan", "MaxWanNegative": "Wan negative prompt"}
