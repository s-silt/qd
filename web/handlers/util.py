#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pylint: disable=broad-exception-raised

import base64
import datetime
import functools
import html
import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from Crypto import Random
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA
from tornado import gen
from tornado.ioloop import IOLoop
from tornado.web import HTTPError, authenticated

import config
from config import delay_max_timeout, strtobool
from libs.log import Log
from libs.security import resolve_blocked_reason
from web.handlers.base import BaseHandler, logger_web_handler

logger_web_util = Log("QD.Web.Util").getlogger()
try:
    import ddddocr  # type: ignore
except ImportError as e:
    if config.display_import_warning:
        logger_web_util.warning(
            'Import DdddOCR module falied: "%s". \nTips: This warning message is only for prompting, it will not affect running of QD framework.',
            e,
        )
    ddddocr = None


def request_parse(req_data):
    """解析请求数据并以json形式返回"""
    if req_data.method == "POST":
        data = req_data.body_arguments
    elif req_data.method == "GET":
        data = req_data.arguments
    return data


class UtilDelayParaHandler(BaseHandler):
    async def get(self):
        try:
            seconds = float(self.get_argument("seconds", 0))
        except Exception as e:
            logger_web_handler.debug(
                "Error, delay 0.0 second: %s", e, exc_info=config.traceback_print
            )
            self.write("Error, delay 0.0 second.")
            return
        if seconds < 0:
            seconds = 0.0
        elif seconds >= delay_max_timeout:
            seconds = delay_max_timeout
            await gen.sleep(seconds)
            self.write("Error, limited by delay_max_timeout, delay {seconds} second.")
            return
        await gen.sleep(seconds)
        self.write(f"delay {seconds} second.")
        return


class UtilDelayIntHandler(BaseHandler):
    async def get(self, seconds):
        try:
            seconds = float(seconds)
        except Exception as e:
            logger_web_handler.debug(
                "Error, delay 0.0 second: %s", e, exc_info=config.traceback_print
            )
            self.write("Error, delay 0.0 second.")
            return
        if seconds < 0:
            seconds = 0.0
        elif seconds > delay_max_timeout:
            seconds = delay_max_timeout
            await gen.sleep(seconds)
            self.write("Error, limited by delay_max_timeout, delay {seconds} second.")
            return
        await gen.sleep(seconds)
        self.write(f"delay {seconds} second.")
        return


class UtilDelayHandler(BaseHandler):
    async def get(self, seconds):
        try:
            seconds = float(seconds)
        except Exception as e:
            logger_web_handler.debug(
                "Error, delay 0.0 second: %s", e, exc_info=config.traceback_print
            )
            self.write("Error, delay 0.0 second.")
            return
        if seconds < 0:
            seconds = 0.0
        elif seconds >= delay_max_timeout:
            seconds = delay_max_timeout
            await gen.sleep(seconds)
            self.write(
                f"Error, limited by {delay_max_timeout}, delay {seconds} second."
            )
            return
        await gen.sleep(seconds)
        self.write(f"delay {seconds} second.")
        return


GMT_FORMAT = "%a, %d %b %Y %H:%M:%S GMT"


class TimeStampHandler(BaseHandler):
    async def get(self):
        rtv = {}
        try:
            ts = self.get_argument("ts", "")
            dt = self.get_argument("dt", "")
            time_format = self.get_argument("form", "%Y-%m-%d %H:%M:%S")
            if not time_format:
                time_format = "%Y-%m-%d %H:%M:%S"
            cst_tz = ZoneInfo("Asia/Shanghai")
            utc_tz = ZoneInfo("UTC")
            tmp = datetime.datetime.fromtimestamp

            if dt:
                ts = datetime.datetime.strptime(dt, time_format).timestamp()

            if ts:
                # 用户时间戳转北京时间
                rtv["完整时间戳"] = float(ts)
                rtv["时间戳"] = int(rtv["完整时间戳"])
                rtv["16位时间戳"] = int(rtv["完整时间戳"] * 1000000)
                rtv["周"] = tmp(rtv["完整时间戳"]).strftime("%w/%W")
                rtv["日"] = "/".join(
                    [
                        tmp(rtv["完整时间戳"]).strftime("%j"),
                        yearday(tmp(rtv["完整时间戳"]).year),
                    ]
                )
                rtv["北京时间"] = tmp(rtv["完整时间戳"], cst_tz).strftime(time_format)
                rtv["GMT格式"] = tmp(rtv["完整时间戳"], utc_tz).strftime(GMT_FORMAT)
                rtv["ISO格式"] = (
                    tmp(rtv["完整时间戳"], utc_tz).isoformat().split("+")[0] + "Z"
                )
            else:
                # 当前本机时间戳, 本机时间和北京时间
                rtv["完整时间戳"] = time.time()
                rtv["时间戳"] = int(rtv["完整时间戳"])
                rtv["16位时间戳"] = int(rtv["完整时间戳"] * 1000000)
                rtv["本机时间"] = tmp(rtv["完整时间戳"]).strftime(time_format)
                rtv["周"] = tmp(rtv["完整时间戳"]).strftime("%w/%W")
                rtv["日"] = "/".join(
                    [
                        tmp(rtv["完整时间戳"]).strftime("%j"),
                        yearday(tmp(rtv["完整时间戳"]).year),
                    ]
                )
                rtv["北京时间"] = tmp(rtv["完整时间戳"], cst_tz).strftime(time_format)
                rtv["GMT格式"] = tmp(rtv["完整时间戳"], utc_tz).strftime(GMT_FORMAT)
                rtv["ISO格式"] = (
                    tmp(rtv["完整时间戳"], utc_tz).isoformat().split("+")[0] + "Z"
                )
            rtv["状态"] = "200"
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))

    async def post(self):
        await self.get()


def yearday(year):
    if (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0):
        return "366"
    else:
        return "365"


class UniCodeHandler(BaseHandler):
    async def get(self):
        rtv = {}
        try:
            content = self.get_argument("content", "")
            html_unescape = self.get_argument("html_unescape", "false")
            tmp = (
                bytes(content, "unicode_escape")
                .decode("utf-8")
                .replace(r"\u", r"\\u")
                .replace(r"\\\u", r"\\u")
            )
            tmp = bytes(tmp, "utf-8").decode("unicode_escape")
            tmp = (
                tmp.encode("utf-8")
                .replace(b"\xc2\xa0", b"\xa0")
                .decode("unicode_escape")
            )
            if strtobool(html_unescape):
                tmp = html.unescape(tmp)
            rtv["转换后"] = tmp
            rtv["状态"] = "200"
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return

    async def post(self):
        rtv = {}
        try:
            content = self.get_argument("content", "")
            html_unescape = self.get_argument("html_unescape", "false")
            tmp = (
                bytes(content, "unicode_escape")
                .decode("utf-8")
                .replace(r"\u", r"\\u")
                .replace(r"\\\u", r"\\u")
            )
            tmp = bytes(tmp, "utf-8").decode("unicode_escape")
            tmp = (
                tmp.encode("utf-8")
                .replace(b"\xc2\xa0", b"\xa0")
                .decode("unicode_escape")
            )
            if strtobool(html_unescape):
                tmp = html.unescape(tmp)
            rtv["转换后"] = tmp
            rtv["状态"] = "200"
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return


class GB2312Handler(BaseHandler):
    async def get(self):
        rtv = {}
        try:
            content = self.get_argument("content", "")
            tmp = urllib.parse.quote(content, encoding="gb2312")
            rtv["转换后"] = tmp
            rtv["状态"] = "200"
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return

    async def post(self):
        rtv = {}
        try:
            content = self.get_argument("content", "")
            tmp = urllib.parse.quote(content, encoding="gb2312")
            rtv["转换后"] = tmp
            rtv["状态"] = "200"
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return


class UrlDecodeHandler(BaseHandler):
    async def get(self):
        rtv = {}
        try:
            content = self.get_argument("content", "")
            encoding = self.get_argument("encoding", "utf-8")
            unquote_plus = self.get_argument("unquote_plus", "false")
            if strtobool(unquote_plus):
                rtv["转换后"] = urllib.parse.unquote_plus(content, encoding=encoding)
            else:
                rtv["转换后"] = urllib.parse.unquote(content, encoding=encoding)
            rtv["状态"] = "200"
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return

    async def post(self):
        rtv = {}
        try:
            content = self.get_argument("content", "")
            encoding = self.get_argument("encoding", "utf-8")
            unquote_plus = self.get_argument("unquote_plus", "false")
            if strtobool(unquote_plus):
                rtv["转换后"] = urllib.parse.unquote_plus(content, encoding=encoding)
            else:
                rtv["转换后"] = urllib.parse.unquote(content, encoding=encoding)
            rtv["状态"] = "200"
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return


class UtilRegexHandler(BaseHandler):
    async def get(self):
        rtv = {}
        try:
            data = self.get_argument("data", "")
            p = self.get_argument("p", "")
            temp = {}
            ds = re.findall(p, data, re.IGNORECASE)
            for cnt, d in enumerate(ds):
                temp[cnt + 1] = d
            rtv["数据"] = temp
            rtv["状态"] = "OK"
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return

    async def post(self):
        rtv = {}
        try:
            data = self.get_argument("data", "")
            p = self.get_argument("p", "")
            temp = {}
            ds = re.findall(p, data, re.IGNORECASE)
            for cnt, d in enumerate(ds):
                temp[cnt + 1] = d
            rtv["数据"] = temp
            rtv["状态"] = "OK"
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))

        return


class UtilStrReplaceHandler(BaseHandler):
    async def get(self):
        rtv = {}
        try:
            s = self.get_argument("s", "")
            p = self.get_argument("p", "")
            t = self.get_argument("t", "")
            rtv["原始字符串"] = s
            rtv["处理后字符串"] = re.sub(p, t, s)
            rtv["状态"] = "OK"
            if self.get_argument("r", "") == "text":
                self.write(html.escape(rtv["处理后字符串"]))
                return
            else:
                self.set_header("Content-Type", "application/json; charset=UTF-8")
                self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
                return
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return

    async def post(self):
        rtv = {}
        try:
            s = self.get_argument("s", "")
            p = self.get_argument("p", "")
            t = self.get_argument("t", "")
            rtv["原始字符串"] = s
            rtv["处理后字符串"] = re.sub(p, t, s)
            rtv["状态"] = "OK"
            if self.get_argument("r", "") == "text":
                self.write(html.escape(rtv["处理后字符串"]))
                return
            else:
                self.set_header("Content-Type", "application/json; charset=UTF-8")
                self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
                return
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return


class UtilRSAHandler(BaseHandler):
    async def get(self):
        try:
            key = self.get_argument("key", "")
            data = self.get_argument("data", "")
            func = self.get_argument("f", "encode")
            if key and data and func:
                lines = ""
                temp = key
                temp = re.findall("-----.*?-----", temp)
                if len(temp) == 2:
                    keytemp = key
                    for t in temp:
                        keytemp = keytemp.replace(t, "")

                    while keytemp:
                        line = keytemp[0:63]
                        lines = lines + line + "\n"
                        keytemp = keytemp.replace(line, "")

                    lines = temp[0] + "\n" + lines + temp[1]

                else:
                    self.write("证书格式错误")
                    return

                cipher_rsa = PKCS1_v1_5.new(RSA.import_key(lines))
                if func.find("encode") > -1:
                    crypt_text = cipher_rsa.encrypt(bytes(data, encoding="utf8"))
                    crypt_text = base64.b64encode(crypt_text).decode("utf8")
                    self.write(crypt_text)
                    return
                elif func.find("decode") > -1:
                    t1 = base64.b64decode(data)
                    decrypt_text = cipher_rsa.decrypt(t1, Random.new().read)
                    decrypt_text = decrypt_text.decode("utf8")
                    self.write(decrypt_text)
                    return
                else:
                    self.write("功能选择错误")
                    return
            else:
                self.write("参数不完整，请确认")
                return
        except Exception as e:
            self.write(str(e))
            return

    async def post(self):
        try:
            key = self.get_argument("key", "")
            data = self.get_argument("data", "")
            func = self.get_argument("f", "encode")
            if key and data and func:
                lines = ""
                for line in key.split("\n"):
                    if line.find("--") < 0:
                        line = line.replace(" ", "+")
                    lines = lines + line + "\n"
                data = data.replace(" ", "+")

                cipher_rsa = PKCS1_v1_5.new(RSA.import_key(lines))
                if func.find("encode") > -1:
                    crypt_text = cipher_rsa.encrypt(bytes(data, encoding="utf8"))
                    crypt_text = base64.b64encode(crypt_text).decode("utf8")
                    self.write(crypt_text)
                    return
                elif func.find("decode") > -1:
                    decrypt_text = cipher_rsa.decrypt(
                        base64.b64decode(data), Random.new().read
                    )
                    decrypt_text = decrypt_text.decode("utf8")
                    self.write(decrypt_text)
                    return
                else:
                    self.write("功能选择错误")
                    return
            else:
                self.write("参数不完整，请确认")
                return
        except Exception as e:
            self.write(str(e))
            return


class ToolboxHandler(BaseHandler):
    async def get(self, userid):
        if self.current_user["isadmin"] or self.check_permission(
            {"userid": int(userid)}, "r"
        ):
            await self.render("toolbox.html", userid=userid)

    async def post(self, userid):
        try:
            email = self.get_argument("email", "")
            pwd = self.get_argument("pwd", "")
            f = self.get_argument("f", "")
            if email and pwd and f:
                async with self.db.transaction() as sql_session:
                    if await self.db.user.challenge_md5(
                        email, pwd, sql_session=sql_session
                    ) or await self.db.user.challenge(
                        email, pwd, sql_session=sql_session
                    ):
                        notepadid = self.get_argument("id_notepad", 1)
                        userid = (
                            await self.db.user.get(
                                email=email, fields=("id",), sql_session=sql_session
                            )
                        )["id"]
                        text_data = (
                            await self.db.notepad.get(
                                userid,
                                notepadid,
                                fields=("content",),
                                sql_session=sql_session,
                            )
                        )["content"]
                        new_data = self.get_argument("data", "")
                        if f.find("write") > -1:
                            text_data = new_data
                            await self.db.notepad.mod(
                                userid,
                                notepadid,
                                content=text_data,
                                sql_session=sql_session,
                            )
                        elif f.find("append") > -1:
                            if text_data is not None:
                                text_data = text_data + "\r\n" + new_data
                            else:
                                text_data = new_data
                            await self.db.notepad.mod(
                                userid,
                                notepadid,
                                content=text_data,
                                sql_session=sql_session,
                            )
                        self.write(text_data)
                        return
                    else:
                        raise Exception("账号密码错误")
            else:
                raise Exception("参数不完整，请确认")
        except Exception as e:
            self.write(str(e))
            return


class ToolboxNotepadHandler(BaseHandler):
    @authenticated
    async def get(self, userid=None, notepadid=1):
        if userid is None:
            raise HTTPError(405)
        if self.current_user["isadmin"] or self.check_permission(
            {"userid": int(userid)}, "r"
        ):
            notepadlist = await self.db.notepad.list(
                fields=("notepadid", "content"),
                limit=config.notepad_limit,
                userid=userid,
            )
            notepadlist.sort(key=lambda x: x["notepadid"])
            if len(notepadlist) == 0:
                if await self.db.user.get(id=userid, fields=("id",)) is not None:
                    await self.db.notepad.add(dict(userid=userid, notepadid=1))
                    notepadlist = await self.db.notepad.list(
                        fields=("notepadid", "content"),
                        limit=config.notepad_limit,
                        userid=userid,
                    )
                else:
                    raise HTTPError(
                        404,
                        log_message="用户不存在或未创建记事本",
                        reason="用户不存在或未创建记事本",
                    )
            if int(notepadid) == 0:
                notepadid = notepadlist[-1]["notepadid"]
            await self.render(
                "toolbox-notepad.html",
                notepad_id=int(notepadid),
                notepad_list=notepadlist,
                userid=userid,
            )
        return

    # @authenticated
    async def post(self, userid=None):
        try:
            email = self.get_argument("email", "")
            pwd = self.get_argument("pwd", "")
            f = self.get_argument("f", "")
            if email and pwd and f:
                async with self.db.transaction() as sql_session:
                    if await self.db.user.challenge_md5(
                        email, pwd, sql_session=sql_session
                    ) or await self.db.user.challenge(
                        email, pwd, sql_session=sql_session
                    ):
                        notepadid = int(self.get_argument("id_notepad", 1))
                        userid = (
                            await self.db.user.get(
                                email=email, fields=("id",), sql_session=sql_session
                            )
                        )["id"]
                        notepad = await self.db.notepad.get(
                            userid,
                            notepadid,
                            fields=("content",),
                            sql_session=sql_session,
                        )
                        if not notepad:
                            if notepadid == 1:
                                await self.db.notepad.add(
                                    dict(userid=userid, notepadid=notepadid),
                                    sql_session=sql_session,
                                )
                            else:
                                raise Exception("记事本不存在")
                        text_data = notepad["content"]
                        new_data = self.get_argument("data", "")
                        if f.find("write") > -1:
                            text_data = new_data
                            await self.db.notepad.mod(
                                userid,
                                notepadid,
                                content=text_data,
                                sql_session=sql_session,
                            )
                        elif f.find("append") > -1:
                            if text_data is not None:
                                text_data = text_data + "\r\n" + new_data
                            else:
                                text_data = new_data
                            await self.db.notepad.mod(
                                userid,
                                notepadid,
                                content=text_data,
                                sql_session=sql_session,
                            )
                        self.write(text_data)
                        return
                    else:
                        raise Exception("账号密码错误")
            else:
                raise Exception("参数不完整，请确认")
        except Exception as e:
            if config.traceback_print:
                traceback.print_exc()
            if str(e).find("get user need id or email") > -1:
                e = "请输入用户名/密码"
            self.write(str(e))
            self.set_status(400)
            logger_web_handler.error(
                "UserID: %s modify Notepad_Toolbox failed! Reason: %s",
                userid or "-1",
                str(e),
            )
            return


class ToolboxNotepadListHandler(BaseHandler):
    async def get(self, userid=None, notepadid=1):
        if userid is None:
            raise HTTPError(405)
        if self.current_user["isadmin"] or self.check_permission(
            {"userid": int(userid)}, "r"
        ):
            notepadlist = await self.db.notepad.list(
                fields=("notepadid", "content"),
                limit=config.notepad_limit,
                userid=userid,
            )
            notepadlist.sort(key=lambda x: x["notepadid"])
            if len(notepadlist) == 0:
                if await self.db.user.get(id=userid, fields=("id",)) is not None:
                    await self.db.notepad.add(dict(userid=userid, notepadid=1))
                    notepadlist = await self.db.notepad.list(
                        fields=("notepadid", "content"),
                        limit=config.notepad_limit,
                        userid=userid,
                    )
                else:
                    raise HTTPError(
                        404,
                        log_message="用户不存在或未创建记事本",
                        reason="用户不存在或未创建记事本",
                    )
            if int(notepadid) == 0:
                notepadid = notepadlist[-1]["notepadid"]
            await self.render(
                "toolbox-notepad.html",
                notepad_id=notepadid,
                notepad_list=notepadlist,
                userid=userid,
            )
        return

    async def post(self, userid=None):
        try:
            email = self.get_argument("email", "")
            pwd = self.get_argument("pwd", "")
            f = self.get_argument("f", "list")
            if email and pwd and f:
                async with self.db.transaction() as sql_session:
                    if await self.db.user.challenge_md5(
                        email, pwd, sql_session=sql_session
                    ) or await self.db.user.challenge(
                        email, pwd, sql_session=sql_session
                    ):
                        userid = (
                            await self.db.user.get(
                                email=email, fields=("id",), sql_session=sql_session
                            )
                        )["id"]
                        notepadid = self.get_argument("id_notepad", "-1")
                        if not notepadid:
                            notepadid = -1
                        else:
                            notepadid = int(notepadid)
                        notepadlist = await self.db.notepad.list(
                            fields=("notepadid",),
                            limit=config.notepad_limit,
                            userid=userid,
                            sql_session=sql_session,
                        )
                        notepadlist = [x["notepadid"] for x in notepadlist]
                        notepadlist.sort()
                        if len(notepadlist) == 0:
                            raise Exception("无法获取该用户记事本编号")
                        if f.find("add") > -1:
                            if len(notepadlist) >= config.notepad_limit:
                                raise Exception(
                                    f"记事本数量超过上限, limit: {config.notepad_limit}"
                                )
                            new_data = self.get_argument("data", "")
                            if new_data == "":
                                new_data = None
                            if notepadid == -1:
                                notepadid = notepadlist[-1] + 1
                            elif notepadid in notepadlist:
                                raise Exception(
                                    f"记事本编号已存在, id_notepad: {notepadid}"
                                )
                            await self.db.notepad.add(
                                dict(
                                    userid=userid, notepadid=notepadid, content=new_data
                                ),
                                sql_session=sql_session,
                            )
                            self.write(f"添加成功, id_notepad: {notepadid}")
                            return
                        elif f.find("delete") > -1:
                            if notepadid > 0:
                                if notepadid not in notepadlist:
                                    raise Exception(
                                        f"记事本编号不存在, id_notepad: {notepadid}"
                                    )
                                if notepadid == 1:
                                    raise Exception("默认记事本不能删除")
                                await self.db.notepad.delete(
                                    userid, notepadid, sql_session=sql_session
                                )
                                self.write(f"删除成功, id_notepad: {notepadid}")
                                return
                            else:
                                raise Exception("id_notepad参数不完整, 请确认")
                        elif f.find("list") > -1:
                            self.write(notepadlist)
                            return
                        else:
                            raise Exception("参数不完整, 请确认")
                    else:
                        raise Exception("账号密码错误")
            else:
                raise Exception("参数不完整, 请确认")
        except Exception as e:
            if config.traceback_print:
                traceback.print_exc()
            if str(e).find("get user need id or email") > -1:
                e = "请输入用户名/密码"
            self.write(str(e))
            self.set_status(400)
            logger_web_handler.error(
                "UserID: %s %s Notepad_Toolbox failed! Reason: %s",
                userid or "-1",
                f,
                str(e),
            )
            return


class DdddOcrServer:
    def __init__(self):
        if ddddocr is not None and hasattr(ddddocr, "DdddOcr"):
            self.oldocr = ddddocr.DdddOcr(old=True, show_ad=False)
            self.ocr = ddddocr.DdddOcr(show_ad=False)
            self.det = ddddocr.DdddOcr(det=True, show_ad=False)
            self.slide = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
            self.extra = {}
            if (
                len(config.extra_onnx_name) == len(config.extra_charsets_name)
                and config.extra_onnx_name[0]
                and config.extra_charsets_name[0]
            ):
                config_dir = os.path.join(
                    os.path.abspath(
                        os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                    ),
                    "config",
                )
                # [#28] onnx 与 charsets 文件名【配对】使用: charsets_path 必须取
                # extra_charsets_name 而非 onnx_name(旧代码误用 onnx_name, 一旦两者
                # 不同名就加载失败)。且每个 extra 模型单独 try/except 隔离, 单个坏模型
                # 不再连坐其它模型 / 整个 OCR。
                for onnx_name, charsets_name in zip(
                    config.extra_onnx_name, config.extra_charsets_name
                ):
                    if not onnx_name or not charsets_name:
                        continue
                    try:
                        self.extra[onnx_name] = ddddocr.DdddOcr(
                            show_ad=False,
                            import_onnx_path=os.path.join(
                                config_dir, f"{onnx_name}.onnx"
                            ),
                            charsets_path=os.path.join(
                                config_dir, f"{charsets_name}.json"
                            ),
                        )
                        logger_web_util.info(
                            "成功加载自定义Onnx模型: %s.onnx (charsets: %s.json)",
                            onnx_name,
                            charsets_name,
                        )
                    except Exception as e:  # noqa: BLE001 - 单模型失败隔离, 不影响其它
                        logger_web_util.warning(
                            "加载自定义Onnx模型失败, 已跳过该模型(不影响其它): %s.onnx, 原因: %s",
                            onnx_name,
                            e,
                        )

    def classification(self, img: bytes, old=False, extra_onnx_name=""):
        if extra_onnx_name:
            return self.extra[extra_onnx_name].classification(img)
        if old:
            return self.oldocr.classification(img)
        else:
            return self.ocr.classification(img)

    def detection(self, img: bytes):
        return self.det.detection(img)

    def slide_match(
        self, imgtarget: bytes, imgbg: bytes, comparison=False, simple_target=False
    ):
        if comparison:
            return self.slide.slide_comparison(imgtarget, imgbg)
        if not simple_target:
            try:
                return self.slide.slide_match(imgtarget, imgbg)
            except Exception as e:
                logger_web_handler.debug(
                    "slide_match error: %s", e, exc_info=config.traceback_print
                )
        return self.slide.slide_match(imgtarget, imgbg, simple_target=True)


# OCR 服务采用【惰性初始化】: 启动 import 阶段【不】加载 onnx 模型。
#
# 背景: 之前这里在模块 import 时就 `DdddOcrServer()`, 而其 __init__ 会同步加载
# 4 个 onnx 模型。onnxruntime 在缺少 AVX/AVX2 指令集的 CPU 上加载模型会直接触发
# SIGILL(Illegal instruction, core dumped)——这是原生崩溃, Python 的 try/except
# 根本拦不住, 会让整个进程在启动 import 阶段就死掉; 配合 docker `restart: unless-stopped`
# 表现为容器秒退 / 反复重启(本仓库 fork 启用 ddddocr 后即出现此现象)。
#
# 改造后:
#   1. 启动不再触碰 onnxruntime, 容器总能正常起来;
#   2. 首次真正用到 OCR 时才构建, 并用 try/except 兜住【Python 级】失败(模型文件缺失、
#      onnxruntime 抛异常等)→ 降级为「OCR 不可用」而非拖垮整个服务;
#   3. 对连"用一次都会 SIGILL"的老 CPU, 可设 ENABLE_DDDDOCR=false 彻底禁用, 永不加载模型。
_ddddocr_singleton: Optional[DdddOcrServer] = None
_ddddocr_init_failed = False
# [#26] onnxruntime 子进程自检结果缓存: None=未探测, True/False=已探测。
_ddddocr_probe_ok: Optional[bool] = None

# [#26] 在【子进程】里真正实例化 onnxruntime 会话并跑一次推理。
# 缺 AVX/AVX2 的 CPU 上 onnxruntime 会触发 SIGILL(Illegal instruction)——这是原生崩溃,
# Python try/except 拦不住。放进子进程后, SIGILL 只会杀死子进程(returncode 非 0),
# 主进程据此判定「本机不可用」并自动降级禁用, 而不会被一起带走。
# 1x1 PNG 仅用于驱动一次推理路径; 推理本身的 Python 级异常被吞掉(我们只关心是否原生崩溃)。
_ONNX_PROBE_CODE = (
    "import base64, ddddocr\n"
    "ocr = ddddocr.DdddOcr(show_ad=False)\n"  # 建会话 = 加载模型(SIGILL 高发点)
    "img = base64.b64decode("
    "'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC')\n"
    "try:\n"
    "    ocr.classification(img)\n"  # 跑一次推理(亦可能 SIGILL)
    "except Exception:\n"
    "    pass\n"
    "print('ONNX_OK')\n"
)


def _probe_onnxruntime_available() -> bool:
    """子进程探测 onnxruntime 能否真正加载/推理而不触发原生崩溃。

    返回 True 表示本机可安全使用 OCR; False 表示应降级禁用。结果带缓存, 仅探测一次。
    """
    global _ddddocr_probe_ok
    if _ddddocr_probe_ok is not None:
        return _ddddocr_probe_ok
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _ONNX_PROBE_CODE],
            capture_output=True,
            timeout=60,
        )
        _ddddocr_probe_ok = proc.returncode == 0
        if not _ddddocr_probe_ok:
            logger_web_util.warning(
                "onnxruntime 子进程自检未通过(returncode=%s), 自动降级禁用验证码 OCR。"
                "常见原因: CPU 缺少 AVX/AVX2 指令集。stderr: %s",
                proc.returncode,
                (proc.stderr or b"")[:500],
            )
    except Exception as e:  # noqa: BLE001 - 自检本身异常一律保守禁用, 不拖垮服务
        logger_web_util.warning(
            "onnxruntime 子进程自检异常, 保守降级禁用验证码 OCR: %s", e
        )
        _ddddocr_probe_ok = False
    return _ddddocr_probe_ok


async def _run_in_executor(func, *args, **kwargs):
    """[#27] 把同步阻塞调用(onnx 加载/推理)丢进线程池, 不阻塞 Tornado IOLoop。"""
    return await IOLoop.current().run_in_executor(
        None, functools.partial(func, *args, **kwargs)
    )


def get_ddddocr_server() -> Optional[DdddOcrServer]:
    """惰性获取 DdddOcrServer 单例。不可用时返回 None(调用方据此回落 HTTP 406)。

    线程安全性: Tornado 单线程事件循环内调用, 无需加锁。
    """
    global _ddddocr_singleton, _ddddocr_init_failed
    if _ddddocr_singleton is not None:
        return _ddddocr_singleton
    if (
        _ddddocr_init_failed
        or not config.enable_ddddocr
        or ddddocr is None
        or not hasattr(ddddocr, "DdddOcr")
    ):
        return None
    # [#26] 默认保持开启, 但先用子进程探测 onnxruntime 是否真的能加载/推理。
    # 无 AVX 的老 CPU 会自动探测失败 → 降级禁用, 可用机正常使用。
    if not _probe_onnxruntime_available():
        _ddddocr_init_failed = True
        return None
    try:
        _ddddocr_singleton = DdddOcrServer()
    except Exception as e:  # noqa: BLE001 - 任何 Python 级初始化失败都应降级, 不能拖垮服务
        _ddddocr_init_failed = True
        logger_web_util.warning(
            "DdddOCR 初始化失败, 已禁用验证码 OCR 功能(不影响框架其它功能): %s", e
        )
        return None
    return _ddddocr_singleton


async def get_img_from_url(imgurl):
    # [D#8/#9] 防 SSRF: 限定 http/https scheme, 请求前对目标主机过 resolve_blocked_reason
    # (复用 libs.security 的统一守卫), 禁用重定向(避免 3xx 跳到受限地址绕过校验),
    # 并移除 verify_ssl=False(不再禁用证书校验)。
    parsed = urllib.parse.urlparse(imgurl)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise HTTPError(400, f"不支持的图片 URL scheme: {scheme or '(空)'}")
    blocked = resolve_blocked_reason(parsed.hostname or "")
    if blocked:
        raise HTTPError(403, f"图片地址被安全策略拦截(防 SSRF): {blocked}")
    async with aiohttp.ClientSession(conn_timeout=config.connect_timeout) as session:
        async with session.get(
            imgurl, timeout=config.request_timeout, allow_redirects=False
        ) as res:
            if res.status in (301, 302, 303, 307, 308):
                raise HTTPError(403, "图片地址发生重定向, 已拒绝(防 SSRF 绕过)")
            content = await res.read()
            base64_data = base64.b64encode(content).decode()
            return base64.b64decode(base64_data)


async def get_img(
    img="",
    imgurl="",
):
    if img:
        # 判断是否为URL
        if img.startswith("http"):
            try:
                return await get_img_from_url(img)
            except Exception as e:
                logger_web_handler.debug(
                    "get_img_from_url error: %s", e, exc_info=config.traceback_print
                )
                return base64.b64decode(img)
        return base64.b64decode(img)
    elif imgurl:
        return await get_img_from_url(imgurl)
    else:
        raise HTTPError(415)


class DdddOcrHandler(BaseHandler):
    @authenticated
    async def get(self):
        self.evil(+1)
        # [#40] 可用性检查移出 try: 不可用时透传 HTTPError(406), 不再被吞成 200。
        server = await _run_in_executor(get_ddddocr_server)
        if not server:
            raise HTTPError(406)
        rtv = {}
        try:
            img = self.get_argument("img", "")
            imgurl = self.get_argument("imgurl", "")
            old = bool(strtobool(self.get_argument("old", "False")))
            extra_onnx_name = self.get_argument("extra_onnx_name", "")
            img = await get_img(img, imgurl)
            # [#27] onnx 推理同步阻塞, 放线程池避免阻塞 IOLoop。
            rtv["Result"] = await _run_in_executor(
                server.classification, img, old=old, extra_onnx_name=extra_onnx_name
            )
            rtv["状态"] = "OK"
        except HTTPError:
            raise  # [#40] 透传非 200 状态(如 get_img 的 415 / SSRF 的 403)
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return

    @authenticated
    async def post(self):
        self.evil(+1)
        server = await _run_in_executor(get_ddddocr_server)
        if not server:
            raise HTTPError(406)
        rtv = {}
        try:
            if self.request.headers.get("Content-Type", "").startswith(
                "application/json"
            ):
                body_dict = json.loads(self.request.body)
                img = body_dict.get("img", "")
                imgurl = body_dict.get("imgurl", "")
                old = bool(strtobool(body_dict.get("old", "False")))
                extra_onnx_name = body_dict.get("extra_onnx_name", "")
            else:
                img = self.get_argument("img", "")
                imgurl = self.get_argument("imgurl", "")
                old = bool(strtobool(self.get_argument("old", "False")))
                extra_onnx_name = self.get_argument("extra_onnx_name", "")

            img = await get_img(img, imgurl)
            rtv["Result"] = await _run_in_executor(
                server.classification, img, old=old, extra_onnx_name=extra_onnx_name
            )
            rtv["状态"] = "OK"
        except HTTPError:
            raise
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=4))
        return


class DdddDetHandler(BaseHandler):
    @authenticated
    async def get(self):
        self.evil(+1)
        server = await _run_in_executor(get_ddddocr_server)
        if not server:
            raise HTTPError(406)
        rtv = {}
        try:
            img = self.get_argument("img", "")
            imgurl = self.get_argument("imgurl", "")
            img = await get_img(img, imgurl)
            rtv["Result"] = await _run_in_executor(server.detection, img)
            rtv["状态"] = "OK"
        except HTTPError:
            raise
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=None))
        return

    @authenticated
    async def post(self):
        self.evil(+1)
        server = await _run_in_executor(get_ddddocr_server)
        if not server:
            raise HTTPError(406)
        rtv = {}
        try:
            if self.request.headers.get("Content-Type", "").startswith(
                "application/json"
            ):
                body_dict = json.loads(self.request.body)
                img = body_dict.get("img", "")
                imgurl = body_dict.get("imgurl", "")
            else:
                img = self.get_argument("img", "")
                imgurl = self.get_argument("imgurl", "")
            img = await get_img(img, imgurl)
            rtv["Result"] = await _run_in_executor(server.detection, img)
            rtv["状态"] = "OK"
        except HTTPError:
            raise
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=None))
        return


class DdddSlideHandler(BaseHandler):
    @authenticated
    async def get(self):
        self.evil(+1)
        server = await _run_in_executor(get_ddddocr_server)
        if not server:
            raise HTTPError(406)
        rtv = {}
        try:
            imgtarget = self.get_argument("imgtarget", "")
            imgbg = self.get_argument("imgbg", "")
            simple_target = bool(
                strtobool(self.get_argument("simple_target", "False"))
            )
            comparison = bool(strtobool(self.get_argument("comparison", "False")))
            imgtarget = await get_img(imgtarget, "")
            imgbg = await get_img(imgbg, "")
            rtv["Result"] = await _run_in_executor(
                server.slide_match,
                imgtarget,
                imgbg,
                comparison=comparison,
                simple_target=simple_target,
            )
            rtv["状态"] = "OK"
        except HTTPError:
            raise
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=None))
        return

    @authenticated
    async def post(self):
        self.evil(+1)
        server = await _run_in_executor(get_ddddocr_server)
        if not server:
            raise HTTPError(406)
        rtv = {}
        try:
            if self.request.headers.get("Content-Type", "").startswith(
                "application/json"
            ):
                body_dict = json.loads(self.request.body)
                imgtarget = body_dict.get("imgtarget", "")
                imgbg = body_dict.get("imgbg", "")
                simple_target = bool(
                    strtobool(body_dict.get("simple_target", "False"))
                )
                comparison = bool(strtobool(body_dict.get("comparison", "False")))
            else:
                imgtarget = self.get_argument("imgtarget", "")
                imgbg = self.get_argument("imgbg", "")
                simple_target = bool(
                    strtobool(self.get_argument("simple_target", "False"))
                )
                comparison = bool(
                    strtobool(self.get_argument("comparison", "False"))
                )

            imgtarget = await get_img(imgtarget, "")
            imgbg = await get_img(imgbg, "")
            rtv["Result"] = await _run_in_executor(
                server.slide_match,
                imgtarget,
                imgbg,
                comparison=comparison,
                simple_target=simple_target,
            )
            rtv["状态"] = "OK"
        except HTTPError:
            raise
        except Exception as e:
            rtv["状态"] = str(e)

        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.write(json.dumps(rtv, ensure_ascii=False, indent=None))
        return


handlers = [
    (r"/util/delay", UtilDelayParaHandler),
    (r"/util/delay/(\d+)", UtilDelayIntHandler),
    (r"/util/delay/(\d+\.\d+)", UtilDelayHandler),
    (r"/util/timestamp", TimeStampHandler),
    (r"/util/unicode", UniCodeHandler),
    (r"/util/urldecode", UrlDecodeHandler),
    (r"/util/gb2312", GB2312Handler),
    (r"/util/regex", UtilRegexHandler),
    (r"/util/string/replace", UtilStrReplaceHandler),
    (r"/util/rsa", UtilRSAHandler),
    (r"/util/toolbox/(\d+)", ToolboxHandler),
    (r"/util/toolbox/notepad", ToolboxNotepadHandler),
    (r"/util/toolbox/(\d+)/notepad", ToolboxNotepadHandler),
    (r"/util/toolbox/(\d+)/notepad/(\d+)", ToolboxNotepadHandler),
    (r"/util/toolbox/notepad/list", ToolboxNotepadListHandler),
    (r"/util/toolbox/(\d+)/notepad/list", ToolboxNotepadListHandler),
    (r"/util/toolbox/(\d+)/notepad/list/(\d+)", ToolboxNotepadListHandler),
    (r"/util/dddd/ocr", DdddOcrHandler),
    (r"/util/dddd/det", DdddDetHandler),
    (r"/util/dddd/slide", DdddSlideHandler),
]
