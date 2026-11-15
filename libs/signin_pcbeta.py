"""The PCBeta daily task and its receipt, read through the forum's normal UI."""

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit

ORIGIN = "https://i.pcbeta.com"
START_URL = f"{ORIGIN}/home.php?mod=task"
RECIPE = "pcbeta_daily_checkin"
STEP_TEXTS = ("找到「每日打卡」", "进入详情并在可用时申请一次", "核对完成时间和 PB 奖励")

_DAILY = "每日打卡"
_DONE = "已完成的任务"
_TIMEOUT = 6500
_LOCAL_TIME = timezone(timedelta(hours=8))
_COMPLETED_AT = re.compile(r"^完成[于於]\s*[:：]?\s*")
_TIMESTAMP = re.compile(
    r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+|今天\s*)"
    r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?![\d:])"
)
_PB = re.compile(r"(?<![A-Z0-9])PB\s*(?:币|幣)?\s*[:：]?\s*([0-9]{1,9})(?![0-9.,])", re.I)


class DailyTaskError(Exception):
    pass


def _task_route(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in ("i.pcbeta.com", "i.pcbeta.com:443")
        or parsed.path != "/home.php"
        or parsed.fragment
        or len(parsed.query) > 128
    ):
        return {}
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(pairs)
    return query if len(query) == len(pairs) and query.get("mod") == "task" else {}


def _task_id(url, action):
    query = _task_route(url)
    task_id = query.get("id", "")
    if query == {"mod": "task", "do": action, "id": task_id} and re.fullmatch(r"[1-9][0-9]{0,9}", task_id):
        return task_id
    return None


async def _visible_link(container, label):
    matches = await container.get_by_role("link", name=label, exact=True).all()
    visible = [link for link in matches if await link.is_visible()]
    if len(visible) > 1:
        raise DailyTaskError(f"远景论坛页面有多个「{label}」链接，无法确认目标")
    return visible[0] if visible else None


async def _link_url(link):
    url, target, download = await link.evaluate(
        """link => [link.href, link.target || document.querySelector('base[target]')?.target || '',
                    link.hasAttribute('download')]""",
        timeout=_TIMEOUT,
    )
    if target.lower() not in ("", "_self") or download:
        raise DailyTaskError("远景论坛任务链接已变化，请核对当前页面")
    return url


async def _open(page, url, check_page):
    await page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT)
    await check_page()


def _is_today(when):
    if when.startswith("今天"):
        return True
    try:
        date = datetime.strptime(when.split()[0].replace("/", "-"), "%Y-%m-%d").date()
    except ValueError:
        return False
    return date == datetime.now(_LOCAL_TIME).date()


async def _receipt(page, completed_url, task_id, check_page):
    if _task_route(page.url) != {"mod": "task", "item": "done"}:
        await _open(page, completed_url, check_page)
    if _task_route(page.url) != {"mod": "task", "item": "done"}:
        raise DailyTaskError("远景论坛未打开已完成的任务列表")
    main = page.locator("#ct .mn")
    done_link = await _visible_link(page.locator("#ct .appl"), _DONE)
    if done_link is None or _task_route(await _link_url(done_link)) != {"mod": "task", "item": "done"}:
        raise DailyTaskError("远景论坛未显示已完成的任务列表")
    link = await _visible_link(main, _DAILY)
    if link is None:
        return None
    completed_id = _task_id(await _link_url(link), "view")
    if completed_id is None or (task_id is not None and completed_id != task_id):
        raise DailyTaskError("已完成列表里的「每日打卡」与当前任务不一致")
    row = link.locator("xpath=ancestor::tr[1]")
    status_cell = row.locator("xpath=./td[last()]")
    if not await status_cell.is_visible():
        return None
    status = (await status_cell.inner_text(timeout=_TIMEOUT)).strip()
    prefix = _COMPLETED_AT.match(status)
    if prefix is None:
        return None
    match = _TIMESTAMP.match(status, prefix.end())
    if match is None:
        # Discuz displays recent completions as “刚刚” or “几分钟前”.
        date_label = status_cell.locator("p > span[title]:visible")
        if await date_label.count() == 1:
            title = await date_label.get_attribute("title", timeout=_TIMEOUT)
            match = _TIMESTAMP.fullmatch((title or "").strip())
    if not match or not _is_today(match[0]):
        return None
    reward_cell = row.locator("xpath=./td[last()-1]")
    reward_text = await reward_cell.inner_text(timeout=_TIMEOUT) if await reward_cell.is_visible() else ""
    reward = _PB.search(reward_text)
    when = " ".join(match[0].split())
    return f"{when}。站点显示奖励 {reward[1]} PB币" if reward else f"{when}。站点未显示 PB 奖励"


async def run_daily_checkin(page, check_page):
    from playwright.async_api import Error as BrowserError

    await check_page()
    await _open(page, START_URL, check_page)
    if _task_route(page.url) not in ({"mod": "task"}, {"mod": "task", "item": "new"}):
        raise DailyTaskError("远景论坛未打开新任务列表")
    main = page.locator("#ct .mn")
    done_link = await _visible_link(page.locator("#ct .appl"), _DONE)
    if done_link is None:
        raise DailyTaskError("远景论坛未显示已完成的任务入口")
    completed_url = await _link_url(done_link)
    if _task_route(completed_url) != {"mod": "task", "item": "done"}:
        raise DailyTaskError("远景论坛已完成的任务入口已变化")

    task_link = await _visible_link(main, _DAILY)
    detail_url = await _link_url(task_link) if task_link is not None else None
    task_id = _task_id(detail_url, "view") if detail_url else None
    if detail_url and task_id is None:
        raise DailyTaskError("远景论坛「每日打卡」详情链接已变化")

    receipt = await _receipt(page, completed_url, task_id, check_page)
    if receipt:
        return f"远景论坛「每日打卡」今日已完成。完成于 {receipt}"
    if detail_url is None:
        raise DailyTaskError("远景论坛的新任务列表没有「每日打卡」，也未查到今天的完成记录")

    await _open(page, detail_url, check_page)
    title = main.get_by_text(_DAILY, exact=True)
    if _task_id(page.url, "view") != task_id or await title.count() != 1 or not await title.is_visible():
        raise DailyTaskError("远景论坛未显示对应的「每日打卡」详情")
    apply = await _visible_link(main, "立即申请")
    if apply is None or not await apply.is_enabled() or _task_id(await _link_url(apply), "apply") != task_id:
        raise DailyTaskError("远景论坛「每日打卡」当前没有可用的「立即申请」入口")
    await check_page()
    try:
        await apply.click(timeout=_TIMEOUT)
    except BrowserError:
        # The forum may already have received the click. Only check its read-only receipt.
        await check_page()
    await check_page()
    receipt = await _receipt(page, completed_url, task_id, check_page)
    if receipt is None:
        raise DailyTaskError("已尝试申请远景论坛「每日打卡」，站点未显示今天的完成记录")
    return f"远景论坛「每日打卡」已完成。完成于 {receipt}"
