import hashlib
import os
import stat
from typing import Optional


def strtobool(val: str):
    """Convert a string representation of truth to true (1) or false (0).

    True values are 'y', 'yes', 't', 'true', 'on', and '1'; false values
    are 'n', 'no', 'f', 'false', 'off', and '0'.  Raises ValueError if
    'val' is anything else.
    """
    val = val.lower()
    if val in ('y', 'yes', 't', 'true', 'on', '1'):
        return 1
    if val in ('n', 'no', 'f', 'false', 'off', '0'):
        return 0
    raise ValueError(f"invalid truth value {val!r}")


def _persist_key(persist_path: str, key: bytes, logger=None) -> None:
    """把密钥原子写入 persist_path, 权限收敛到 0600。失败仅告警, 不阻断启动。"""
    try:
        parent = os.path.dirname(persist_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = persist_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(key)
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600; Windows 上基本是 no-op
        except OSError:
            pass
        os.replace(tmp, persist_path)
    except OSError as e:
        if logger:
            logger.warning(
                "持久化密钥到 %s 失败, 本次使用内存密钥(重启可能变化): %s", persist_path, e
            )


def resolve_persistent_key(
    *,
    env_value: Optional[str],
    persist_path: str,
    legacy_seed: str,
    data_exists: bool,
    logger=None,
) -> bytes:
    """解析一个 32 字节密钥, 兼顾"安全默认"与"不破坏已有部署"。

    优先级:
      1. env_value 非空        -> sha256(env_value)(用户显式配置, 行为不变)
      2. persist_path 已有密钥  -> 读回(稳定, 不每次重启都换)
      3. 否则生成并持久化:
         - data_exists=True   -> sha256(legacy_seed) 沿用旧默认密钥, 保住已加密数据/登录态,
                                  同时告警建议手动轮换;
         - data_exists=False  -> os.urandom(32) 随机密钥, 全新部署默认即安全。
    """
    if env_value:
        return hashlib.sha256(env_value.encode("utf-8")).digest()

    try:
        with open(persist_path, "rb") as f:
            existing = f.read()
        if len(existing) == 32:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        pass  # 读失败不阻断, 回退到生成逻辑

    if data_exists:
        key = hashlib.sha256(legacy_seed.encode("utf-8")).digest()
        if logger:
            logger.warning(
                "[安全] 检测到已有数据但未设置密钥, 暂沿用默认弱密钥以保证数据可解。"
                "强烈建议设置环境变量并迁移数据后轮换密钥(详见安全文档)。"
            )
    else:
        key = os.urandom(32)
        if logger:
            logger.info("[安全] 全新部署: 已生成随机密钥并持久化到 %s", persist_path)

    _persist_key(persist_path, key, logger)
    return key
