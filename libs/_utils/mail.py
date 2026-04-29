#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# vim: set et sw=4 ts=4 sts=4 ff=unix fenc=utf8:
# Mail / SMTP helpers extracted from libs/utils.py
# pylint: disable=broad-exception-raised

import smtplib
from email.mime.text import MIMEText
from urllib import parse as urllib_parse

from tornado import httpclient

import config
from libs.log import Log
from libs._utils.jinja_filters import utf8

logger_util = Log('QD.Http.Util').getlogger()


async def send_mail(to, subject, text=None, html=None, shark=False, _from=f"QD提醒 <noreply@{config.domain}>"):
    if not config.mailgun_key:
        subtype = 'html' if html else 'plain'
        await _send_mail(to, subject, html or text or '', subtype)
        return

    httpclient.AsyncHTTPClient.configure('tornado.curl_httpclient.CurlAsyncHTTPClient')
    if shark:
        client = httpclient.AsyncHTTPClient()
    else:
        client = httpclient.HTTPClient()

    body = {
        'from': utf8(_from),
        'to': utf8(to),
        'subject': utf8(subject),
    }

    if text:
        body['text'] = utf8(text)
    elif html:
        body['html'] = utf8(html)
    else:
        raise Exception('need text or html')

    req = httpclient.HTTPRequest(
        method="POST",
        url=f"https://api.mailgun.net/v3/{config.mailgun_domain}/messages",
        auth_username="api",
        auth_password=config.mailgun_key,
        body=urllib_parse.urlencode(body)
    )
    res = await client.fetch(req)
    return res


async def _send_mail(to, subject, text=None, subtype='html'):
    if not config.mail_smtp:
        logger_util.info('no smtp')
        return
    msg = MIMEText(text, _subtype=subtype, _charset='utf-8')
    msg['Subject'] = subject
    msg['From'] = config.mail_from
    msg['To'] = to
    try:
        logger_util.info('send mail to %s', to)
        tls_established = False

        # Create SMTP connection according to the configuration
        if config.mail_starttls:  # use starttls
            s = smtplib.SMTP(config.mail_smtp, config.mail_port or 587)
            try:
                s.starttls()
                tls_established = True
            except smtplib.SMTPException as e:
                logger_util.error("smtp starttls failed: %s", e, exc_info=config.traceback_print)
        if not tls_established:
            if config.mail_ssl:
                s = smtplib.SMTP_SSL(config.mail_smtp, config.mail_port or 465)
            else:
                s = smtplib.SMTP(config.mail_smtp, config.mail_port or 25)

        try:
            # Only attempt login if user and password are set
            if config.mail_user and config.mail_password:
                s.login(config.mail_user, config.mail_password)
            s.sendmail(config.mail_from, to, msg.as_string())
        except smtplib.SMTPException as e:
            logger_util.error("smtp sendmail error: %s", e, exc_info=config.traceback_print)
        finally:
            # If sending fails, still close the connection
            s.quit()

    except Exception as e:
        logger_util.error('error occurred while sending mail: %s', e, exc_info=config.traceback_print)
    return
