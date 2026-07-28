"""GPU-resident ONNX tagger used while RUN CAPTURE receives frames."""

import csv
import json
import os
from pathlib import Path

import numpy as np
from huggingface_hub import hf_hub_download
from PIL import Image


DEFAULT_REPO_ID = "Mooshie/mobilenetv4_conv_aa_large.dbv4-full"


class MobileNetV4Tagger:
    """A single CUDA ONNX session reused for every frame in one RUN CAPTURE."""

    def __init__(self, repo_id: str = DEFAULT_REPO_ID):
        self.repo_id = repo_id
        self.session = None
        self.tags = None
        self.categories = None

    def preload(self) -> None:
        if self.session is not None:
            return
        # PyTorch, installed by the project setup, provides the CUDA/cuDNN DLLs
        # required by the GPU ONNX Runtime wheel on Windows.
        try:
            import torch
            torch_lib = Path(torch.__file__).resolve().parent / "lib"
            if torch_lib.exists() and hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(torch_lib))
        except ImportError:
            pass
        import onnxruntime as ort

        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
        available = ort.get_available_providers()
        if "CUDAExecutionProvider" not in available:
            raise RuntimeError(f"TAGGER needs CUDAExecutionProvider; available: {available}")
        model_path = hf_hub_download(self.repo_id, "model.onnx")
        self.session = ort.InferenceSession(
            model_path,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        with open(hf_hub_download(self.repo_id, "selected_tags.csv"), newline="", encoding="utf-8") as file:
            self.tags = list(csv.DictReader(file))
        with open(hf_hub_download(self.repo_id, "categories.json"), encoding="utf-8") as file:
            self.categories = {str(row["category"]): row["name"] for row in json.load(file)}

    @staticmethod
    def _prepare(image_path: Path) -> np.ndarray:
        with Image.open(image_path) as source:
            image = source.convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        background.alpha_composite(image)
        image = background.convert("RGB")
        ratio = min(512 / image.width, 512 / image.height)
        resized = image.resize((round(image.width * ratio), round(image.height * ratio)), Image.BILINEAR)
        padded = Image.new("RGB", (512, 512), "white")
        padded.paste(resized, ((512 - resized.width) // 2, (512 - resized.height) // 2))
        ratio = 448 / min(padded.size)
        resized = padded.resize((round(padded.width * ratio), round(padded.height * ratio)), Image.BICUBIC)
        left = (resized.width - 448) // 2
        top = (resized.height - 448) // 2
        array = np.asarray(resized.crop((left, top, left + 448, top + 448)), dtype=np.float32)
        array = array.transpose(2, 0, 1) / 255.0
        mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)[:, None, None]
        std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)[:, None, None]
        return ((array - mean) / std)[None, ...]

    def _to_tags(self, prediction: np.ndarray) -> dict[str, dict[str, float]]:
        result = {"rating": {}, "general": {}, "character": {}}
        for index, row in enumerate(self.tags):
            category = self.categories[str(row["category"])]
            result[category][row["name"]] = float(prediction[index])
        return result

    def predict_many(self, image_paths: list[Path]) -> list[dict[str, dict[str, float]]]:
        """Run one CUDA inference for up to five completed capture frames."""
        if not image_paths:
            return []
        self.preload()
        input_name = self.session.get_inputs()[0].name
        output_names = [output.name for output in self.session.get_outputs()]
        batch = np.concatenate([self._prepare(image_path) for image_path in image_paths], axis=0)
        output_values = self.session.run(output_names, {input_name: batch})
        predictions = dict(zip(output_names, output_values))["prediction"]
        return [self._to_tags(prediction) for prediction in predictions]

    def predict(self, image_path: Path) -> dict[str, dict[str, float]]:
        return self.predict_many([image_path])[0]


def score_for(tags: dict[str, float], wanted: str) -> float:
    wanted = wanted.replace("_", " ").strip().lower()
    for name, score in tags.items():
        if name.replace("_", " ").strip().lower() == wanted:
            return score
    return 0.0
