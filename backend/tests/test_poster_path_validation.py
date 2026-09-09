"""海报代理路径校验单测。"""
import pytest

from app.services.poster import _validate_poster_path

VALID = "/t/p/w500/ab12cd.jpg"


@pytest.mark.parametrize("path", [
    VALID,
    "/t/p/w500/x.jpg",
    "/t/p/original/x%20y.jpg",
])
def test_valid_paths(path):
    assert _validate_poster_path(path) is True


@pytest.mark.parametrize("path", [
    "",
    "   ",
    "t/p/w500/x.jpg",          # 不以 / 开头
    "../etc/passwd",
    "/t/p/../../x.jpg",        # 归一化越界
    "/t/p/w500/../..",
    "http://evil.com/x.jpg",
    "https://image.tmdb.org/t/p/w500/x.jpg",  # 完整 URL：协议段由 "://" 拦截
    "//host/t/p/x.jpg",
    "/t/p/x.jpg\\..\\..",
    "/t/p/x\x00.jpg",
    "/t/p",                    # 无子资源路径
    "/t/p/",
    "/t/p/..",
    "%2e%2e%2fetc%2fpasswd",   # 解码后为 ../etc/passwd
])
def test_invalid_paths(path):
    assert _validate_poster_path(path) is False
