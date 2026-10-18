#!/usr/bin/env python3

import importlib.util
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "transcribe_qwen.py"
SPEC = importlib.util.spec_from_file_location("transcribe_qwen", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RoutingAndNormalizationTests(unittest.TestCase):
    def test_route_boundary(self):
        self.assertEqual(MODULE.choose_route(300), ("direct_base64", MODULE.SHORT_MODEL))
        self.assertEqual(
            MODULE.choose_route(300.001), ("temporary_storage", MODULE.LONG_MODEL)
        )

    def test_short_nested_output(self):
        response = {
            "output": {
                "output": {
                    "text": "你好。",
                    "sentences": [
                        {
                            "speaker_id": 0,
                            "begin_time": 10,
                            "end_time": 500,
                            "text": "你好。",
                        }
                    ],
                }
            }
        }
        result = MODULE.normalize_short(response, 1.0)
        self.assertEqual(result["text"], "你好。")
        self.assertEqual(result["segments"][0]["speaker_id"], 0)

    def test_long_result_normalization(self):
        transcript = {
            "transcripts": [
                {
                    "text": "甲说。乙答。",
                    "sentences": [
                        {"speaker_id": 0, "begin_time": 0, "end_time": 500, "text": "甲说。"},
                        {"speaker_id": 1, "begin_time": 500, "end_time": 900, "text": "乙答。"},
                    ],
                }
            ]
        }
        result = MODULE.normalize_long(transcript, {"usage": {"duration": 1}}, 1.0)
        self.assertEqual(result["text"], "甲说。乙答。")
        self.assertEqual([item["speaker_id"] for item in result["segments"]], [0, 1])
        self.assertEqual(result["temporary_audio_retention_hours"], 48)


if __name__ == "__main__":
    unittest.main()
