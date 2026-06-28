# -*- coding: utf-8 -*-
"""TDD: 密钥解析 resolve_persistent_key —— 向后兼容的持久化随机密钥。

目标(P0-C 安全加固, 但不得破坏已有部署):
  - 显式 env 设置 -> 用 env(行为不变)
  - env 未设 + 已有数据 -> 沿用 legacy('binux')派生并持久化, 保证已加密数据可解、登录不掉
  - env 未设 + 全新安装 -> 生成随机密钥并持久化(新部署默认即安全)
  - 一旦持久化, 后续读回同一密钥(稳定, 不每次重启都换)
"""
import hashlib

from libs.config_utils import resolve_persistent_key


def test_env_set_uses_env_and_does_not_persist(tmp_path):
    p = tmp_path / "cookie_secret"
    key = resolve_persistent_key(
        env_value="mysecret", persist_path=str(p), legacy_seed="binux", data_exists=True
    )
    assert key == hashlib.sha256(b"mysecret").digest()
    assert not p.exists(), "显式 env 不应写持久化文件"


def test_empty_env_treated_as_unset(tmp_path):
    p = tmp_path / "cookie_secret"
    key = resolve_persistent_key(
        env_value="", persist_path=str(p), legacy_seed="binux", data_exists=True
    )
    # 空 env 视为未设, 落到"保数据"分支
    assert key == hashlib.sha256(b"binux").digest()
    assert p.exists()


def test_existing_data_preserves_legacy_and_persists(tmp_path):
    p = tmp_path / "aes_key"
    key = resolve_persistent_key(
        env_value=None, persist_path=str(p), legacy_seed="binux", data_exists=True
    )
    assert key == hashlib.sha256(b"binux").digest(), "已有数据必须沿用旧密钥, 否则解不开"
    assert p.exists()
    key2 = resolve_persistent_key(
        env_value=None, persist_path=str(p), legacy_seed="binux", data_exists=True
    )
    assert key2 == key, "第二次应读回同一密钥"


def test_fresh_install_generates_random_and_persists(tmp_path):
    p = tmp_path / "aes_key"
    key = resolve_persistent_key(
        env_value=None, persist_path=str(p), legacy_seed="binux", data_exists=False
    )
    assert len(key) == 32
    assert key != hashlib.sha256(b"binux").digest(), "全新安装不应再用默认密钥"
    assert p.exists()
    key2 = resolve_persistent_key(
        env_value=None, persist_path=str(p), legacy_seed="binux", data_exists=False
    )
    assert key2 == key, "随机密钥须持久化稳定"


def test_persisted_file_takes_precedence(tmp_path):
    p = tmp_path / "aes_key"
    first = resolve_persistent_key(
        env_value=None, persist_path=str(p), legacy_seed="binux", data_exists=False
    )
    # 即便之后 data_exists=True, 已持久化的随机密钥仍优先(不回退到 legacy)
    again = resolve_persistent_key(
        env_value=None, persist_path=str(p), legacy_seed="binux", data_exists=True
    )
    assert again == first
