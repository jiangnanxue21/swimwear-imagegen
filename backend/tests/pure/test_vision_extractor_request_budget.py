"""属性识别的整份多模态请求预算。

真实兼容端点限制的是最终 JSON,不是原图文件。这里复刻评分链路已经验证过的
三道防线:同源序列化计数、只在超限时压缩、发送前复核。
"""
from __future__ import annotations

import asyncio
import io
import random

from app.extractors.schema import (
    api_ready_schema,
    build_extraction_prompt,
    build_extraction_schema,
)
from app.extractors.vision import (
    API_STYLE_CHAT,
    API_STYLE_RESPONSES,
    ExtractorModelConfig,
    VisionAttributeExtractor,
)
from app.llm.images import PreparedImage, to_data_url
from app.providers.errors import ProviderInputError
from tests.pure._helpers import expect_raises, temporary_config


def _run(awaitable):
    return asyncio.run(awaitable)


def _extractor(*, style: str = API_STYLE_CHAT, max_request_bytes: int):
    return VisionAttributeExtractor(
        ExtractorModelConfig(
            base_url="https://api.example.com/v1",
            model="qwen-vl",
            api_key="k",
            api_style=style,
            max_request_bytes=max_request_bytes,
        )
    )


def _request_kwargs():
    fields = ("garment_type", "primary_color", "back_style")
    return {
        "prompt": build_extraction_prompt(fields),
        "schema": api_ready_schema(build_extraction_schema(fields)),
        "idempotency_key": "same-paid-call",
    }


def _noise_png() -> bytes:
    from PIL import Image

    raw = random.Random(898).randbytes(768 * 768 * 3)
    buffer = io.BytesIO()
    Image.frombytes("RGB", (768, 768), raw).save(buffer, format="PNG")
    return buffer.getvalue()


def test_default_total_budget_stays_below_the_known_six_mib_endpoint_limit():
    config = ExtractorModelConfig()
    assert config.max_request_bytes == 5 * 1024 * 1024
    assert config.max_request_bytes < 6 * 1024 * 1024


def test_total_budget_reads_its_own_extractor_setting():
    with temporary_config(EXTRACTOR_MODEL_MAX_REQUEST_MB=4):
        config = ExtractorModelConfig.from_settings()
    assert config.max_request_bytes == 4 * 1024 * 1024


def test_a_request_that_already_fits_keeps_the_original_image_bytes():
    image = PreparedImage(
        role="PRODUCT_IMAGE",
        mime_type="image/png",
        byte_size=3,
        data_url="data:image/png;base64,AAA",
    )
    extractor = _extractor(max_request_bytes=5 * 1024 * 1024)

    request = _run(extractor._fit_request_to_budget(image=image, **_request_kwargs()))

    assert request.images[0] is image
    assert request.images[0].payload_url() == "data:image/png;base64,AAA"


def test_oversized_inline_image_is_fitted_for_both_realtime_api_styles():
    payload = _noise_png()
    image = PreparedImage(
        role="PRODUCT_IMAGE",
        mime_type="image/png",
        byte_size=len(payload),
        data_url=to_data_url(payload, "image/png"),
    )
    limit = 1_200_000

    for style in (API_STYLE_CHAT, API_STYLE_RESPONSES):
        extractor = _extractor(style=style, max_request_bytes=limit)
        request = _run(
            extractor._fit_request_to_budget(image=image, **_request_kwargs())
        )

        assert extractor._request_body_bytes(request) <= limit
        assert request.images[0].describe()["compacted"] is True
        assert request.images[0].byte_size < image.byte_size


def test_the_real_extraction_path_sends_only_the_fitted_request():
    payload = _noise_png()
    image = PreparedImage(
        role="PRODUCT_IMAGE",
        mime_type="image/png",
        byte_size=len(payload),
        data_url=to_data_url(payload, "image/png"),
    )
    limit = 1_200_000
    extractor = _extractor(max_request_bytes=limit)
    sent = []

    async def fake_send(request, *, send_once, on_attempt):
        sent.append(request)
        on_attempt(1)
        return {
            "model": "qwen-vl",
            "choices": [
                {
                    "message": {
                        "content": '{"fields":[],"unreadable_fields":[],"missing":[]}'
                    }
                }
            ],
        }, 200

    extractor._transport.send = fake_send
    _run(
        extractor.extract_async(
            image=image,
            target_fields=("primary_color",),
            enum_defs={},
            options={},
        )
    )

    assert len(sent) == 1
    assert extractor._request_body_bytes(sent[0]) <= limit
    assert sent[0].images[0].describe()["compacted"] is True


def test_non_image_json_overhead_is_counted_and_rejected_before_http():
    extractor = _extractor(max_request_bytes=1024)
    public = PreparedImage(
        role="PRODUCT_IMAGE",
        mime_type="image/jpeg",
        byte_size=0,
        url="https://cdn.example.com/product.jpg",
    )

    err = expect_raises(
        ProviderInputError,
        lambda: _run(
            extractor._fit_request_to_budget(
                prompt="x" * 2048,
                image=public,
                schema={},
                idempotency_key=None,
            )
        ),
    )

    assert "请求体" in err.message
    assert err.detail["request_body_bytes"] > err.detail["limit_bytes"]
    assert err.detail["inline_count"] == 0
