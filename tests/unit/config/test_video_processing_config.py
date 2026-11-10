# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The video-processing knob is typed once, in the Google provider module."""

from typing import get_args

from pydantic import ValidationError
import pytest

from artemis.config.agent import VideoAnalyzerConfig
from artemis.llm.google import VideoProcessing


def test_processing_field_reuses_the_provider_literal():
    assert VideoAnalyzerConfig.model_fields["processing"].annotation is VideoProcessing
    assert set(get_args(VideoProcessing)) == {"auto", "agentic", "static"}


def test_processing_defaults_to_auto_and_accepts_every_mode():
    assert VideoAnalyzerConfig().processing == "auto"
    for mode in get_args(VideoProcessing):
        assert VideoAnalyzerConfig(processing=mode).processing == mode


def test_processing_rejects_unknown_modes():
    with pytest.raises(ValidationError, match="processing"):
        VideoAnalyzerConfig(processing="bogus")


@pytest.mark.parametrize(
    ("recording", "tools", "expected"),
    [
        ("false", None, False),
        ("0", None, False),
        (None, "false", False),
        ("true", None, True),
        (None, "1", True),
        ("false", "true", False),  # the primary variable wins when both are set
    ],
)
def test_video_recording_env_override(monkeypatch, recording, tools, expected):
    from artemis.config.agent import AgentGlobalConfig

    for name, value in (
        ("ARTEMIS_VIDEO_RECORDING_ENABLED", recording),
        ("ARTEMIS_WITH_VIDEO_RECORDING_TOOLS", tools),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    data = AgentGlobalConfig.synchronize_profiles_and_components({})
    assert data["video_analyzer"]["enabled"] is expected
    assert data["pro"]["video_analyzer"]["enabled"] is expected
