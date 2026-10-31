#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
南京邮电大学 (NJUPT) 校园网自动登录脚本
================================================
基于 Dr.COM (城市热点) eportal 门户认证协议。
支持 Windows / Linux / NAS (飞牛 OS 等) 系统。

认证流程 (源自登录日志分析):
  1. 获取客户端内网 IP (socket 或从认证页面解析)
  2. 调用 loadConfig 获取 program_index / page_index (IP 需 base64 编码)
  3. 调用 login 完成认证 (IP 明文, 账号格式: ,0,用户名@运营商后缀)
  4. 解析 JSONP 响应判断成功/失败

依赖: 仅 Python 3 标准库, 无需安装任何第三方包。
用法见文件末尾或运行: python njupt_login.py --help

敏感信息 (用户名/密码) 通过命令行参数、环境变量或配置文件传入, 不硬编码。
"""

import os
import re
import sys
import json
import time
import base64
import random
import socket
import argparse
import urllib.request
import urllib.parse
import urllib.error
import ssl
import configparser
from datetime import datetime


# ---------------------------------------------------------------------------
# 常量定义
# ---------------------------------------------------------------------------

SCRIPT_VERSION = "1.0.0"

# 默认认证服务器 (从 HAR 日志中分析得到)
DEFAULT_SERVER_HOST = "p.njupt.edu.cn"
DEFAULT_SERVER_PORT = 804

# 运营商后缀映射: 用户友好名称 -> 账号后缀
# 账号完整格式: ,0,<用户名>@<后缀>
OPERATOR_MAP = {
    "cmcc":     "cmcc",      # 中国移动
    "telecom":  "telecom",   # 中国电信
    "unicom":   "unicom",    # 中国联通
    "default":  "",          # 校园网默认 (无后缀)
    "njupt":    "",          # 校园网默认 (别名)
}

# JSONP 回调名 (与页面一致)
CALLBACK_LOADCONFIG = "dr1001"
CALLBACK_ONLINE_LIST = "dr1002"
CALLBACK_LOGIN = "dr1003"

# 浏览器 UA (用于伪装正常浏览器请求)
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 登录成功后用于验证网络连通性的测试 URL (HTTP, 便于捕获重定向)
CONNECTIVITY_TEST_URL = "http://www.baidu.com/"

# 请求超时 (秒)
REQUEST_TIMEOUT = 10


# ---------------------------------------------------------------------------
# 日志输出 (带时间戳, 适配 NAS 定时任务查看)
# ---------------------------------------------------------------------------

class Logger:
    """简易日志器, 同时输出到 stdout, 方便在 NAS / cron 中查看。"""

    def __init__(self, verbose=False):
        self.verbose = verbose

    def _ts(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def info(self, msg):
        print(f"[{self._ts()}] [INFO ] {msg}", flush=True)

    def warn(self, msg):
        print(f"[{self._ts()}] [WARN ] {msg}", flush=True)

    def error(self, msg):
        print(f"[{self._ts()}] [ERROR] {msg}", flush=True)

    def ok(self, msg):
        print(f"[{self._ts()}] [ OK  ] {msg}", flush=True)

    def debug(self, msg):
        if self.verbose:
            print(f"[{self._ts()}] [DEBUG] {msg}", flush=True)


# ---------------------------------------------------------------------------
# HTTPS 上下文 (校园网自签名证书, 需禁用校验)
# ---------------------------------------------------------------------------

def _make_ssl_context():
    """创建不校验证书的 SSL 上下文 (校园网认证网关普遍使用自签名证书)。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_SSL_CTX = _make_ssl_context()


# ---------------------------------------------------------------------------
# JSONP 响应解析
# ---------------------------------------------------------------------------

def parse_jsonp(text):
    """
    从 JSONP 响应文本中提取 JSON 对象。
    例: 'dr1003({"result":1,"msg":"Portal协议认证成功！"})' -> {"result":1,...}
    """
    if not text:
        return None
    # 匹配 callback({...}) 形式
    m = re.search(r"\((\{.*\})\)\s*$", text.strip(), re.DOTALL)
    if not m:
        # 也许直接是 JSON
        try:
            return json.loads(text.strip())
        except Exception:
            return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 认证客户端
# ---------------------------------------------------------------------------

class PortalClient:
    """Dr.COM eportal 认证客户端。"""

    def __init__(self, host, port, ua, log):
        self.host = host
        self.port = int(port)
        self.ua = ua
        self.log = log
        # 认证服务器基础 URL
        self.base = f"https://{host}:{port}/eportal/portal"
        # 认证服务器根 URL (用于获取认证页面)
        self.root = f"https://{host}"

    # -- 底层请求 --------------------------------------------------------

    def _get(self, url, referer=None):
        """发起 GET 请求, 返回响应文本 (禁用自动重定向以便分析)。"""
        self.log.debug(f"GET {url}")
        req = urllib.request.Request(url)
        req.add_header("User-Agent", self.ua)
        req.add_header("Accept", "*/*")
        if referer:
            req.add_header("Referer", referer)
        try:
            # 不跟随重定向: 用自定义 opener
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=_SSL_CTX),
                _NoRedirectHandler(),
            )
            resp = opener.open(req, timeout=REQUEST_TIMEOUT)
            data = resp.read()
            # 尝试解码
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                text = data.decode(charset, errors="replace")
            except (LookupError, TypeError):
                text = data.decode("utf-8", errors="replace")
            return resp.status, resp, text
        except urllib.error.HTTPError as e:
            # 对于重定向类 HTTP 错误, 也返回内容
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return e.code, e, body
        except urllib.error.URLError as e:
            raise ConnectionError(f"网络请求失败: {e.reason}") from e
        except ssl.SSLError as e:
            raise ConnectionError(f"SSL 错误 (证书/协议): {e}") from e
        except socket.timeout:
            raise ConnectionError("请求超时, 请检查网络或服务器地址") from None
        except Exception as e:
            raise ConnectionError(f"请求异常: {e}") from e

    # -- 获取客户端 IP ----------------------------------------------------

    def get_local_ip(self):
        """通过 socket 获取本机内网 IP (连接认证服务器选择本地接口)。"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 不需要真正建立连接, 仅让 OS 选择本地出口接口
            s.connect((self.host, 443))
            ip = s.getsockname()[0]
        except Exception as e:
            self.log.debug(f"socket 获取 IP 失败: {e}")
            ip = None
        finally:
            s.close()
        return ip

    def get_portal_page_ip(self):
        """
        访问认证服务器根路径, 从返回页面解析 v46ip (服务器视角的客户端 IP)。
        这是最准确的 IP 来源 (与 loadConfig 中服务器看到的 IP 一致)。
        """
        try:
            status, resp, text = self._get(self.root + "/", referer=self.root + "/")
        except ConnectionError as e:
            self.log.debug(f"访问认证页面失败: {e}")
            return None
        if not text:
            return None
        # 匹配 v46ip='10.161.240.179' (可能有空格)
        m = re.search(r"v46ip\s*=\s*['\"]([^'\"]+)['\"]", text)
        if m:
            ip = m.group(1).strip()
            # 简单校验是否像 IP
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                return ip
        # 备用: 匹配 mip=010161240179 格式 (去掉前导0的 IP)
        m = re.search(r"mip\s*=\s*['\"]?(\d{12})['\"]?", text)
        if m:
            raw = m.group(1)
            try:
                parts = [str(int(raw[i:i + 3])) for i in range(0, 12, 3)]
                return ".".join(parts)
            except Exception:
                pass
        return None

    # -- 加载页面配置 -----------------------------------------------------

    def load_config(self, ip):
        """
        调用 loadConfig 获取 program_index / page_index。
        参数 wlan_user_ip 需 base64 编码。
        返回 dict 或 None。
        """
        ip_b64 = base64.b64encode(ip.encode()).decode()
        params = {
            "callback": CALLBACK_LOADCONFIG,
            "program_index": "",
            "wlan_vlan_id": "0",
            "wlan_user_ip": ip_b64,
            "wlan_user_ipv6": "",
            "wlan_user_ssid": "",
            "wlan_user_areaid": "",
            "wlan_ac_ip": "",
            "wlan_ap_mac": "000000000000",
            "gw_id": "000000000000",
            "page_index": "",
            "jsVersion": "4.X",
            "v": str(random.randint(1000, 9999)),
            "lang": "zh",
        }
        url = self.base + "/page/loadConfig?" + urllib.parse.urlencode(params)
        try:
            status, resp, text = self._get(url, referer=self.root + "/")
        except ConnectionError as e:
            self.log.warn(f"loadConfig 请求失败: {e}")
            return None
        data = parse_jsonp(text)
        if not data:
            self.log.debug(f"loadConfig 原始响应: {text[:200]}")
            return None
        if data.get("code") == 1 and "data" in data:
            return data["data"]
        self.log.debug(f"loadConfig 返回: {data}")
        return None

    # -- 查询在线状态 -----------------------------------------------------

    def check_online(self, ip):
        """
        查询当前 IP 是否已在线。
        返回 (is_online: bool, msg: str)
        """
        ip_b64 = base64.b64encode(ip.encode()).decode()
        params = {
            "callback": CALLBACK_ONLINE_LIST,
            "user_account": "",
            "user_password": "",
            "wlan_user_mac": "000000000000",
            "wlan_user_ip": ip_b64,
            "wlan_user_ipv6": "",
            "jsVersion": "4.X",
            "program_index": "",
            "page_index": "",
            "v": str(random.randint(1000, 9999)),
            "lang": "zh",
        }
        url = self.base + "/online_list?" + urllib.parse.urlencode(params)
        try:
            status, resp, text = self._get(url, referer=self.root + "/")
        except ConnectionError as e:
            self.log.warn(f"在线状态查询失败: {e}")
            return False, "查询失败"
        data = parse_jsonp(text)
        if not data:
            return False, "无法解析响应"
        # result=0 表示未在线; result=1 表示已在线
        if data.get("result") == 1:
            return True, data.get("msg", "已在线")
        return False, data.get("msg", "未在线")

    # -- 登录 -------------------------------------------------------------

    def login(self, username, password, operator_suffix, ip,
              program_index="", page_index=""):
        """
        执行登录认证。
        返回 (success: bool, msg: str, raw: dict)
        """
        # 拼接账号: ,0,<用户名>@<后缀>
        account = f",0,{username}@{operator_suffix}" if operator_suffix else f",0,{username}"

        # 随机参数
        rcn = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=8))
        v = str(random.randint(1000, 9999))

        params = {
            "callback": CALLBACK_LOGIN,
            "login_method": "1",
            "is_base64encode": "0",
            "user_account": account,
            "user_password": password,
            "wlan_user_ip": ip,
            "wlan_user_ipv6": "",
            "wlan_user_mac": "000000000000",
            "wlan_vlan_id": "0",
            "wlan_ac_ip": "",
            "wlan_ac_name": "",
            "authex_enable": "",
            "jsVersion": "4.5",
            "terminal_type": "1",
            "lang": "zh-cn",
            "user_agent": self.ua,
            "enable_r3": "0",
            "mac_type": "0",
            "rcn": rcn,
            "operate": "portal_login",
            "business_type": "1",
            "program_index": program_index,
            "page_index": page_index,
            "v": v,
            "lang": "zh",
        }
        url = self.base + "/login?" + urllib.parse.urlencode(params)
        try:
            status, resp, text = self._get(url, referer=self.root + "/")
        except ConnectionError as e:
            return False, f"登录请求失败: {e}", {}

        data = parse_jsonp(text)
        if not data:
            self.log.debug(f"login 原始响应: {text[:300]}")
            return False, "无法解析登录响应", {}

        result = data.get("result")
        msg = data.get("msg", "")
        if result == 1:
            return True, msg or "认证成功", data
        return False, msg or "认证失败", data

    # -- 网络连通性验证 ---------------------------------------------------

    def verify_connectivity(self):
        """登录后访问外网验证网络是否真正连通。返回 bool。"""
        try:
            req = urllib.request.Request(CONNECTIVITY_TEST_URL,
                                         headers={"User-Agent": self.ua})
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=_SSL_CTX),
                _NoRedirectHandler(),
            )
            resp = opener.open(req, timeout=REQUEST_TIMEOUT)
            # 正常返回 200 说明已联网; 如果被重定向到认证页则未联网
            final_url = resp.geturl()
            if "p.njupt.edu.cn" in final_url or "eportal" in final_url:
                return False
            return resp.status == 200
        except urllib.error.HTTPError as e:
            # 3xx 重定向到认证页面 = 未联网
            loc = e.headers.get("Location", "")
            if "p.njupt.edu.cn" in loc or "eportal" in loc:
                return False
            return e.code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 不跟随重定向的 Handler
# ---------------------------------------------------------------------------

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随 3xx 重定向, 以便分析认证状态。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 抛出 HTTPError 让上层捕获, 而不是自动跳转
        raise urllib.error.HTTPError(newurl, code, msg, headers, fp)


# ---------------------------------------------------------------------------
# 配置加载 (命令行参数 > 环境变量 > 配置文件 > 默认值)
# ---------------------------------------------------------------------------

class Config:
    """聚合所有运行配置。"""

    def __init__(self):
        self.username = ""
        self.password = ""
        self.operator = "cmcc"
        self.server_host = DEFAULT_SERVER_HOST
        self.server_port = DEFAULT_SERVER_PORT
        self.ip = ""            # 手动指定 IP (可选)
        self.ua = DEFAULT_UA
        self.check_only = False # 仅检测在线状态
        self.verify = True      # 登录后验证连通性
        self.verbose = False

    def validate(self):
        if not self.check_only:
            if not self.username:
                return "缺少用户名 (username)"
            if not self.password:
                return "缺少密码 (password)"
        if self.operator not in OPERATOR_MAP:
            return f"未知运营商: {self.operator} (可选: {', '.join(OPERATOR_MAP)})"
        return None


def load_config(args):
    """按优先级合并配置来源。"""
    cfg = Config()
    cfg.verbose = args.verbose
    cfg.check_only = args.check
    cfg.verify = not args.no_verify

    # 1. 配置文件 (最低优先级)
    if args.config:
        if not os.path.isfile(args.config):
            raise FileNotFoundError(f"配置文件不存在: {args.config}")
        cp = configparser.ConfigParser()
        cp.read(args.config, encoding="utf-8")
        if cp.has_section("auth"):
            cfg.username = cp.get("auth", "username", fallback=cfg.username)
            cfg.password = cp.get("auth", "password", fallback=cfg.password)
            cfg.operator = cp.get("auth", "operator", fallback=cfg.operator)
        if cp.has_section("server"):
            cfg.server_host = cp.get("server", "host", fallback=cfg.server_host)
            cfg.server_port = cp.getint("server", "port", fallback=cfg.server_port)
            cfg.ip = cp.get("server", "ip", fallback=cfg.ip)
            cfg.ua = cp.get("server", "user_agent", fallback=cfg.ua)

    # 默认配置文件路径 (同目录 config.ini)
    if not args.config:
        default_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")
        if os.path.isfile(default_cfg):
            cp = configparser.ConfigParser()
            try:
                cp.read(default_cfg, encoding="utf-8")
                if cp.has_section("auth"):
                    cfg.username = cp.get("auth", "username", fallback=cfg.username)
                    cfg.password = cp.get("auth", "password", fallback=cfg.password)
                    cfg.operator = cp.get("auth", "operator", fallback=cfg.operator)
                if cp.has_section("server"):
                    cfg.server_host = cp.get("server", "host", fallback=cfg.server_host)
                    cfg.server_port = cp.getint("server", "port", fallback=cfg.server_port)
                    cfg.ip = cp.get("server", "ip", fallback=cfg.ip)
                    cfg.ua = cp.get("server", "user_agent", fallback=cfg.ua)
            except Exception:
                pass

    # 2. 环境变量
    cfg.username = os.environ.get("NJUPT_USERNAME", cfg.username)
    cfg.password = os.environ.get("NJUPT_PASSWORD", cfg.password)
    cfg.operator = os.environ.get("NJUPT_OPERATOR", cfg.operator)
    cfg.server_host = os.environ.get("NJUPT_SERVER", cfg.server_host)
    cfg.ip = os.environ.get("NJUPT_IP", cfg.ip)

    # 3. 命令行参数 (最高优先级)
    if args.username:
        cfg.username = args.username
    if args.password:
        cfg.password = args.password
    if args.operator:
        cfg.operator = args.operator
    if args.server:
        cfg.server_host = args.server
    if args.port:
        cfg.server_port = args.port
    if args.ip:
        cfg.ip = args.ip
    if args.ua:
        cfg.ua = args.ua

    return cfg


# ---------------------------------------------------------------------------
# 失败原因诊断
# ---------------------------------------------------------------------------

def diagnose_failure(msg):
    """根据登录失败消息给出可能的原因与建议。"""
    m = (msg or "").lower()
    hints = []
    if any(k in msg for k in ["密码", "password", "password_error", "认证失败"]):
        hints.append("密码错误: 请检查密码是否正确, 注意大小写与特殊字符。")
    if any(k in msg for k in ["账号", "account", "用户不存在", "不存在"]):
        hints.append("账号错误: 请检查用户名与运营商选择是否匹配 (如学号是否需带 @cmcc 等)。")
    if any(k in msg for k in ["停机", "停用", "禁用", "暂停", "欠费", "余额"]):
        hints.append("账号状态异常: 账号可能已停机/欠费, 请前往自助服务平台充值或激活。")
    if any(k in msg for k in ["已在线", "already", "重复", "在线", "重复登录"]):
        hints.append("账号已在线: 可能已在其他设备登录, 可先注销后再试。")
    if any(k in msg for k in ["流量", "时长", "超出", "用尽", "超量"]):
        hints.append("套餐流量/时长已用尽: 请充值或更换套餐。")
    if any(k in msg for k in ["IP", "ip", "地址", "绑定", "mac"]):
        hints.append("IP/MAC 绑定问题: 请尝试手动指定 --ip 参数, 或联系网络中心解绑。")
    if any(k in msg for k in ["限制", "并发", "数量"]):
        hints.append("并发连接数超限: 请先下线其他设备。")
    if not hints:
        hints.append("未知错误: 请检查网络连接、账号密码及服务器地址是否正确。")
        hints.append("可加 --verbose 查看详细日志, 或在浏览器手动登录对比。")
    return hints


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(cfg):
    log = Logger(verbose=cfg.verbose)
    log.info(f"NJUPT 校园网自动登录脚本 v{SCRIPT_VERSION}")
    log.info(f"服务器: {cfg.server_host}:{cfg.server_port} | 运营商: {cfg.operator}")

    client = PortalClient(cfg.server_host, cfg.server_port, cfg.ua, log)

    # ---- 第 1 步: 获取客户端 IP ----
    ip = cfg.ip
    if not ip:
        log.info("正在获取客户端 IP ...")
        # 优先从认证页面解析 (服务器视角, 最准确)
        ip = client.get_portal_page_ip()
        if ip:
            log.debug(f"从认证页面解析到 IP: {ip}")
        if not ip:
            ip = client.get_local_ip()
            if ip:
                log.debug(f"从 socket 获取本机 IP: {ip}")
        if not ip:
            log.error("无法获取客户端 IP。请使用 --ip 参数手动指定内网 IP (如 10.x.x.x)。")
            return 3
    else:
        log.info(f"使用指定 IP: {ip}")

    log.ok(f"客户端 IP: {ip}")

    # ---- 第 2 步: 检测是否已在线 ----
    log.info("检测当前在线状态 ...")
    online, online_msg = client.check_online(ip)
    if online:
        log.ok(f"当前 IP 已在线: {online_msg} (无需重复登录)")
        if cfg.verify:
            if client.verify_connectivity():
                log.ok("网络连通性验证通过, 可正常访问外网。")
            else:
                log.warn("在线状态显示已登录, 但外网连通性验证未通过 (可能需要等待或已掉线)。")
        return 0
    log.info(f"当前未在线: {online_msg}")

    # 仅检测模式: 到此结束
    if cfg.check_only:
        log.info("仅检测模式, 不执行登录。")
        return 0

    # ---- 第 3 步: 加载页面配置 (获取 program_index / page_index) ----
    log.info("加载认证页面配置 ...")
    page_cfg = client.load_config(ip)
    program_index = ""
    page_index = ""
    if page_cfg:
        program_index = page_cfg.get("program_index", "")
        page_index = page_cfg.get("page_index", "")
        log.debug(f"program_index={program_index}, page_index={page_index}")
        log.ok("页面配置加载成功")
    else:
        log.warn("未能获取页面配置 (program_index/page_index), 将尝试空值登录。")

    # ---- 第 4 步: 执行登录 ----
    operator_suffix = OPERATOR_MAP.get(cfg.operator, "")
    log.info(f"正在以账号 {cfg.username}@{operator_suffix or '默认'} 登录 ...")
    success, msg, raw = client.login(
        username=cfg.username,
        password=cfg.password,
        operator_suffix=operator_suffix,
        ip=ip,
        program_index=program_index,
        page_index=page_index,
    )

    if success:
        log.ok(f"登录成功! {msg}")
        # ---- 第 5 步: 验证网络连通性 ----
        if cfg.verify:
            log.info("验证网络连通性 ...")
            time.sleep(1)  # 等待认证生效
            if client.verify_connectivity():
                log.ok("网络连通性验证通过, 已成功联网!")
            else:
                log.warn("登录返回成功, 但外网连通性验证未通过。")
                log.warn("可能原因: 认证尚未生效(等待几秒)、DNS 未刷新、或仍处于认证网段。")
        return 0
    else:
        log.error(f"登录失败: {msg}")
        log.info("可能的原因与建议:")
        for h in diagnose_failure(msg):
            print(f"    - {h}", flush=True)
        return 1


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="njupt_login",
        description="南京邮电大学 (NJUPT) 校园网自动登录脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
配置优先级: 命令行参数 > 环境变量 > 配置文件 > 默认值

环境变量:
  NJUPT_USERNAME   用户名
  NJUPT_PASSWORD   密码
  NJUPT_OPERATOR   运营商 (cmcc/telecom/unicom/default)
  NJUPT_SERVER     认证服务器地址
  NJUPT_IP         手动指定客户端 IP

配置文件 (config.ini) 示例:
  [auth]
  username = B20240001
  password = your_password
  operator = cmcc

  [server]
  host = p.njupt.edu.cn
  port = 804
  ; ip = 10.161.240.179   ; 可选, 手动指定

NAS / Linux 定时任务 (已联网时每 5 分钟检测, 掉线自动重连):
  */5 * * * * /usr/bin/python3 /path/to/njupt_login.py --config /path/to/config.ini >> /var/log/njupt_login.log 2>&1

运营商说明:
  cmcc     中国移动
  telecom  中国电信
  unicom   中国联通
  default  校园网默认 (无后缀)
""",
    )
    p.add_argument("-u", "--username", help="校园网账号 (学号/工号)")
    p.add_argument("-p", "--password", help="校园网密码")
    p.add_argument("-o", "--operator", choices=list(OPERATOR_MAP.keys()),
                   help="运营商 (默认: cmcc)")
    p.add_argument("--server", help=f"认证服务器地址 (默认: {DEFAULT_SERVER_HOST})")
    p.add_argument("--port", type=int, help=f"认证服务器端口 (默认: {DEFAULT_SERVER_PORT})")
    p.add_argument("--ip", help="手动指定客户端内网 IP (自动获取失败时使用)")
    p.add_argument("--ua", help="自定义 User-Agent")
    p.add_argument("-c", "--config", help="配置文件路径 (默认: 同目录 config.ini)")
    p.add_argument("--check", action="store_true", help="仅检测在线状态, 不执行登录")
    p.add_argument("--no-verify", action="store_true", help="跳过登录后的网络连通性验证")
    p.add_argument("-v", "--verbose", action="store_true", help="输出详细调试日志")
    p.add_argument("--version", action="version", version=f"njupt_login {SCRIPT_VERSION}")
    return p


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2

    err = cfg.validate()
    if err:
        parser.error(err)

    try:
        return run(cfg)
    except KeyboardInterrupt:
        print("\n[INFO] 用户中断", file=sys.stderr)
        return 130
    except ConnectionError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        print("[提示] 请确认: 1) 已连接校园网 WiFi/有线; 2) 服务器地址正确; "
              "3) 网络未被完全阻断。", file=sys.stderr)
        return 4
    except Exception as e:
        print(f"[ERROR] 未预期错误: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 5


if __name__ == "__main__":
    sys.exit(main())
