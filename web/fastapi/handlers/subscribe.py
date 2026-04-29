#!/usr/bin/env python
# -*- encoding: utf-8 -*-
"""
FastAPI port of web/handlers/subscribe.py.

Original Tornado handlers mapped here:
  SubscribeHandler           GET  /subscribe/{userid}/
  SubscribeUpdatingHandler   WS   /subscribe/{userid}/updating/   (TODO: WebSocket — see below)
  SubscribeRefreshHandler    GET|POST /subscribe/refresh/{userid}/
  SubscribSignupReposHandler GET|POST /subscribe/signup_repos/{userid}/
  GetReposInfoHandler        POST /subscribe/{userid}/get_reposinfo
  UnsubscribeReposHandler    GET|POST /subscribe/unsubscribe_repos/{userid}/
  ToggleRepoAccHandler       POST /subscribe/toggle_acc/{userid}/

WebSocket (SubscribeUpdatingHandler):
  TODO: The WebSocket update flow relies on a class-level connection registry
  (SubscribeUpdatingHandler.users) shared across all connections, plus
  `send_global_message` broadcasting to every open socket.  Full porting
  requires a separate async task manager / connection manager that is outside
  the scope of Phase-2 HTTP work.  The WS endpoint below is a minimal
  skeleton that accepts the connection, returns the "already updating" status,
  and closes — sufficient for smoke tests.  Full implementation is deferred.
"""

import asyncio
import base64
import json
import random
import time
from typing import Dict, Optional
from urllib.parse import quote

try:
    import aiohttp  # type: ignore  # optional — only needed for WS update flow
    _AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None  # type: ignore
    _AIOHTTP_AVAILABLE = False

import config
from config import proxies
from libs.log import Log

try:
    from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, RedirectResponse
except ImportError:
    # Allow static analysis / import in non-fastapi environments
    APIRouter = object  # type: ignore
    Depends = lambda f: f  # type: ignore
    HTTPException = Exception  # type: ignore
    Request = object  # type: ignore
    WebSocket = object  # type: ignore
    WebSocketDisconnect = Exception  # type: ignore
    HTMLResponse = object  # type: ignore
    RedirectResponse = object  # type: ignore

from web.fastapi.base import get_current_user, get_db, require_user
from web.fastapi.templates import render_template

logger = Log("QD.Web.Handler.Subscribe").getlogger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_admin(user: dict, userid: int) -> bool:
    """Return True when the user is the owner AND has admin role."""
    return user["id"] == userid and user.get("role") == "admin"


def _require_owner_or_admin(user: dict, userid: int) -> None:
    """Raise 403 if user is neither the userid-owner nor admin."""
    if user["id"] != userid:
        raise HTTPException(status_code=403, detail="Forbidden: userid not match")


# ---------------------------------------------------------------------------
# WebSocket connection registry (mirrors SubscribeUpdatingHandler class vars)
# ---------------------------------------------------------------------------

class _WSRegistry:
    """Minimal shared state for the WS updating endpoint."""
    users: Dict[int, WebSocket] = {}
    updating: bool = False
    updating_start_time: int = 0


_ws_registry = _WSRegistry()


# ---------------------------------------------------------------------------
# GET /subscribe/{userid}/
# ---------------------------------------------------------------------------

@router.get("/subscribe/{userid}/")
async def subscribe_get(
    request: Request,
    userid: int,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """
    Render public-template subscription page for a user.

    If repos were last updated more than 24 h ago, renders the wait/update
    page instead of the main subscription list.
    """
    _require_owner_or_admin(user, userid)
    adminflg = _is_admin(user, userid)
    msg = ""

    try:
        site_row = await db.site.get(1, fields=("repos",))
        repos = json.loads(site_row["repos"])
        tpls = await db.pubtpl.list()

        if int(time.time()) - int(repos["lastupdate"]) > 24 * 3600:
            return render_template(
                request,
                "pubtpl_wait.html",
                tpls=tpls,
                user=user,
                userid=user["id"],
                adminflg=adminflg,
                repos=repos["repos"],
                msg=msg,
            )

        return render_template(
            request,
            "pubtpl_subscribe.html",
            tpls=tpls,
            user=user,
            userid=user["id"],
            adminflg=adminflg,
            repos=repos["repos"],
            msg=msg,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "UserID: %s browse Subscribe failed! Reason: %s",
            userid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        try:
            tpls = await db.pubtpl.list()
            site_row = await db.site.get(1, fields=("repos",))
            repos_data = json.loads(site_row["repos"])
            return render_template(
                request,
                "pubtpl_subscribe.html",
                tpls=tpls,
                user=user,
                userid=user["id"],
                adminflg=adminflg,
                repos=repos_data.get("repos", []),
                msg=str(e),
            )
        except Exception:
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /subscribe/{userid}  (no trailing slash — redirect)
# ---------------------------------------------------------------------------

@router.get("/subscribe/{userid}")
async def subscribe_get_noslash(
    request: Request,
    userid: int,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Redirect /subscribe/{userid} → /subscribe/{userid}/."""
    return RedirectResponse(url=f"/subscribe/{userid}/", status_code=307)


# ---------------------------------------------------------------------------
# WebSocket /subscribe/{userid}/updating/
# TODO: Full implementation deferred — see module docstring.
# ---------------------------------------------------------------------------

@router.websocket("/subscribe/{userid}/updating/")
async def subscribe_updating_ws(
    websocket: WebSocket,
    userid: int,
    db=Depends(get_db),
):
    """
    WebSocket endpoint for live template-repo update progress.

    TODO: Full implementation deferred (Phase 2 HTTP scope).

    Current skeleton:
    - Validates auth via the 'user' secure cookie before accepting.
    - Sends an initial status message.
    - If an update is already running, reports status; otherwise sends TODO message.
    - Full broadcast/registry implementation is pending.
    """
    from web.fastapi.auth import decode_signed_value
    import umsgpack  # type: ignore

    # ---- auth before accept ----
    raw_cookie = websocket.cookies.get("user")
    user = None
    if raw_cookie:
        raw_bytes = decode_signed_value("user", raw_cookie, max_age_days=config.cookie_days)
        if raw_bytes:
            try:
                user = umsgpack.unpackb(raw_bytes)
            except Exception:
                user = None

    if not user:
        await websocket.close(code=4403)
        return

    if user["id"] != userid:
        await websocket.close(code=4403)
        return

    adminflg = user.get("role") == "admin"

    if (
        not adminflg
        and len(_ws_registry.users) >= config.websocket.max_connections_subscribe
    ):
        await websocket.close(code=4429)
        return

    await websocket.accept()

    # Deduplicate: close previous connection for same userid
    if userid in _ws_registry.users:
        try:
            await _ws_registry.users[userid].close(1001)
        except Exception:
            pass
        del _ws_registry.users[userid]

    _ws_registry.users[userid] = websocket

    try:
        if _ws_registry.updating and (
            int(time.time()) - _ws_registry.updating_start_time <= 60
        ):
            await websocket.send_json({"code": 1000, "message": "正在更新中..."})
        else:
            await websocket.send_json({"code": 1000, "message": "开始更新..."})
            # TODO: run the full update flow here (ported from SubscribeUpdatingHandler.update)
            await websocket.send_json({"code": 0, "message": "TODO: 完整更新逻辑待移植"})

        # Keep alive — drain incoming messages until client disconnects
        while True:
            try:
                _ = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        pass
    finally:
        if _ws_registry.users.get(userid) is websocket:
            del _ws_registry.users[userid]


# ---------------------------------------------------------------------------
# GET|POST /subscribe/refresh/{userid}/
# ---------------------------------------------------------------------------

@router.get("/subscribe/refresh/{userid}/")
@router.post("/subscribe/refresh/{userid}/")
async def subscribe_refresh(
    request: Request,
    userid: int,
    op: Optional[str] = None,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """
    Admin: reset lastupdate timestamp so next WS connection triggers a
    fresh repo fetch.  With op=clear, also wipes all pubtpl rows.
    """
    # op may come from query param or form body
    if op is None:
        try:
            form = await request.form()
            op = form.get("op", "")
        except Exception:
            op = request.query_params.get("op", "")

    try:
        if not (user["id"] == userid and user.get("role") == "admin"):
            raise Exception("没有权限操作")
        if not op:
            raise Exception("op参数为空")

        async with db.transaction() as sql_session:
            site_row = await db.site.get(1, fields=("repos",), sql_session=sql_session)
            repos = json.loads(site_row["repos"])
            repos["lastupdate"] = 0
            await db.site.mod(
                1,
                repos=json.dumps(repos, ensure_ascii=False, indent=4),
                sql_session=sql_session,
            )
            if op == "clear":
                for pubtpl in await db.pubtpl.list(fields=("id",), sql_session=sql_session):
                    await db.pubtpl.delete(pubtpl["id"], sql_session=sql_session)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "UserID: %s refresh Subscribe failed! Reason: %s",
            userid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        return render_template(
            request, "utils_run_result.html",
            log=str(e), title="设置失败", flg="danger",
        )

    return RedirectResponse(url=f"/subscribe/{int(userid)}/", status_code=303)


# ---------------------------------------------------------------------------
# GET|POST /subscribe/signup_repos/{userid}/
# ---------------------------------------------------------------------------

@router.get("/subscribe/signup_repos/{userid}/")
async def subscribe_signup_repos_get(
    request: Request,
    userid: int,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Admin: render the repo-registration form."""
    if _is_admin(user, userid):
        return render_template(request, "pubtpl_register.html", userid=userid)
    else:
        logger.error(
            "UserID: %s browse Subscrib_signup_repos failed! Reason: 非管理员用户，不可设置",
            userid,
        )
        return render_template(
            request, "utils_run_result.html",
            log="非管理员用户，不可设置", title="设置失败", flg="danger",
        )


@router.post("/subscribe/signup_repos/{userid}/")
async def subscribe_signup_repos_post(
    request: Request,
    userid: int,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Admin: register a new template repository."""
    try:
        if not _is_admin(user, userid):
            raise Exception("非管理员用户，不可设置")

        form = await request.form()
        env: dict = {}
        for k, v in form.multi_items():
            val = v if not isinstance(v, bytes) else v.decode()
            if val == "false":
                env[k] = False
            elif val == "true":
                env[k] = True
            else:
                env[k] = val

        if not (env.get("reponame") and env.get("repourl") and env.get("repobranch")):
            raise Exception("仓库名/url/分支不能为空")

        site_row = await db.site.get(1, fields=("repos",))
        repos = json.loads(site_row["repos"])

        for repo in repos["repos"]:
            if repo["reponame"] == env["reponame"]:
                raise Exception("已存在同名仓库")

        repos["repos"].append(env)
        repos["lastupdate"] = 0
        await db.site.mod(1, repos=json.dumps(repos, ensure_ascii=False, indent=4))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "UserID: %s modify Subscribe_signup_repos failed! Reason: %s",
            userid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        return render_template(
            request, "utils_run_result.html",
            log=str(e), title="设置失败", flg="danger",
        )

    return render_template(
        request, "utils_run_result.html",
        log="设置成功，请关闭操作对话框或刷新页面查看",
        title="设置成功", flg="success",
    )


# ---------------------------------------------------------------------------
# POST /subscribe/{userid}/get_reposinfo
# ---------------------------------------------------------------------------

@router.post("/subscribe/{userid}/get_reposinfo")
async def get_reposinfo(
    request: Request,
    userid: int,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Admin: return rendered info for a selected set of repos."""
    try:
        if not _is_admin(user, userid):
            raise Exception("非管理员用户，不可查看")

        form = await request.form()
        tmp = json.loads((await db.site.get(1, fields=("repos",)))["repos"])["repos"]
        repos = []
        for repoid_str, selected in form.multi_items():
            if isinstance(selected, bytes):
                selected = selected.decode()
            if selected == "true":
                repos.append(tmp[int(repoid_str)])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "UserID: %s get Subscribe_Repos_Info failed! Reason: %s",
            userid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        return render_template(
            request, "utils_run_result.html",
            log=str(e), title="获取信息失败", flg="danger",
        )

    return render_template(request, "pubtpl_reposinfo.html", repos=repos)


# ---------------------------------------------------------------------------
# GET|POST /subscribe/unsubscribe_repos/{userid}/
# ---------------------------------------------------------------------------

@router.get("/subscribe/unsubscribe_repos/{userid}/")
async def unsubscribe_repos_get(
    request: Request,
    userid: int,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Admin: render the repo-unsubscribe form."""
    try:
        if not _is_admin(user, userid):
            raise Exception("非管理员用户，不可设置")
        return render_template(request, "pubtpl_unsubscribe.html", user=user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "UserID: %s browse UnSubscribe_Repos failed! Reason: %s",
            userid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        return render_template(
            request, "utils_run_result.html",
            log=str(e), title="打开失败", flg="danger",
        )


@router.post("/subscribe/unsubscribe_repos/{userid}/")
async def unsubscribe_repos_post(
    request: Request,
    userid: int,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Admin: remove selected repos and their associated pubtpl rows."""
    try:
        if not _is_admin(user, userid):
            raise Exception("非管理员用户，不可设置")

        form = await request.form()
        env: dict = {}
        for k, v in form.multi_items():
            val = v if not isinstance(v, bytes) else v.decode()
            try:
                env[k] = json.loads(val)
            except Exception:
                env[k] = val

        async with db.transaction() as sql_session:
            site_row = await db.site.get(1, fields=("repos",), sql_session=sql_session)
            repos = json.loads(site_row["repos"])
            tmp = repos["repos"]
            result = []
            selected_repos = env.get("selectedrepos", {})
            if isinstance(selected_repos, str):
                try:
                    selected_repos = json.loads(selected_repos)
                except Exception:
                    selected_repos = {}

            for i, repo in enumerate(tmp):
                if not selected_repos.get(str(i), False):
                    result.append(repo)
                else:
                    pubtpls = await db.pubtpl.list(
                        reponame=repo["reponame"],
                        fields=("id",),
                        sql_session=sql_session,
                    )
                    for pubtpl in pubtpls:
                        await db.pubtpl.delete(pubtpl["id"], sql_session=sql_session)
            repos["repos"] = result
            await db.site.mod(
                1,
                repos=json.dumps(repos, ensure_ascii=False, indent=4),
                sql_session=sql_session,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "UserID: %s unsubscribe Subscribe_Repos failed! Reason: %s",
            userid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        return render_template(
            request, "utils_run_result.html",
            log=str(e), title="设置失败", flg="danger",
        )

    return render_template(
        request, "utils_run_result.html",
        log="设置成功，请关闭操作对话框或刷新页面查看",
        title="设置成功", flg="success",
    )


# ---------------------------------------------------------------------------
# POST /subscribe/toggle_acc/{userid}/
# ---------------------------------------------------------------------------

@router.post("/subscribe/toggle_acc/{userid}/")
async def toggle_repo_acc(
    request: Request,
    userid: int,
    user: dict = Depends(require_user),
    db=Depends(get_db),
):
    """Admin: toggle the acceleration flag for a repo by index."""
    try:
        if not _is_admin(user, userid):
            raise Exception("非管理员用户，不可设置")

        form = await request.form()
        repo_id_str = form.get("repo_id", "")
        repo_acc_str = form.get("repo_acc", "").lower()

        if not repo_id_str:
            raise Exception("仓库ID为空")

        repo_acc = repo_acc_str == "true"

        async with db.transaction() as sql_session:
            site_row = await db.site.get(1, fields=("repos",), sql_session=sql_session)
            repos = json.loads(site_row["repos"])

            try:
                repo_id = int(repo_id_str)
                if repo_id < 0 or repo_id >= len(repos["repos"]):
                    raise ValueError("仓库ID超出范围")
            except (ValueError, IndexError) as ve:
                raise Exception(f"操作失败: {ve}")

            repo_name = repos["repos"][repo_id]["reponame"]
            repos["repos"][repo_id]["repoacc"] = repo_acc

            await db.site.mod(
                1,
                repos=json.dumps(repos, ensure_ascii=False, indent=4),
                sql_session=sql_session,
            )

        logger.info(
            "UserID: %s toggle repo '%s' accelerate to '%s' success",
            userid, repo_name, "on" if repo_acc else "off",
        )
        return render_template(
            request, "utils_run_result.html",
            log=f"已成功{'开启' if repo_acc else '关闭'}仓库 {repo_name} 的加速",
            title="设置成功", flg="success",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "UserID: %s toggle repo accelerate failed! Reason: %s",
            userid,
            str(e).replace("\\r\\n", "\r\n"),
            exc_info=config.traceback_print,
        )
        return render_template(
            request, "utils_run_result.html",
            log=str(e), title="设置失败", flg="danger",
        )
