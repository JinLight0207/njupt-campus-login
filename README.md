# 南京邮电大学校园网自动登录脚本

> 南京邮电大学 (NJUPT) 校园网 Dr.COM eportal 认证的自动登录工具，支持 Windows / Linux / NAS（飞牛 OS 等），纯 Python 标准库实现，零第三方依赖。

## 功能特性

- **自动认证**：自动完成校园网 Portal 认证并联网，无需手动打开浏览器
- **自动获取 IP**：优先从认证页面解析客户端内网 IP（服务器视角，最准确），失败回退 socket 获取，亦可手动指定
- **掉线自动重连**：配合定时任务可实现掉线自动重连（NAS / 路由器 / 服务器常驻运行）
- **智能状态检测**：登录前检测是否已在线，避免重复登录；登录后验证外网连通性
- **失败诊断**：认证失败时根据返回信息给出具体原因与建议（密码错误、账号停机、已在线、流量用尽、IP 绑定等）
- **跨平台**：Windows / Linux / NAS（飞牛 OS、群晖等）通用，已处理校园网自签名证书
- **零依赖**：仅使用 Python 3 标准库，无需 `pip install` 任何包
- **安全**：账号密码不硬编码，通过命令行参数 / 环境变量 / 配置文件传入

## 快速开始

### 1. 准备

```bash
# 复制配置模板并填写账号信息
cp config.ini.example config.ini
```

编辑 `config.ini`：

```ini
[auth]
username = B20240001        # 学号 / 工号
password = your_password    # 校园网密码
operator = cmcc             # 运营商: cmcc(移动) / telecom(电信) / unicom(联通) / default(校园网)

[server]
host = p.njupt.edu.cn
port = 804
ip =                        # 留空自动获取, 失败时手动填内网 IP (如 10.161.240.179)
```

### 2. 运行

```bash
python3 njupt_login.py                  # 使用 config.ini
python3 njupt_login.py -u B20240001 -p yourpass -o cmcc   # 命令行直接登录
python3 njupt_login.py --check          # 仅检测在线状态
python3 njupt_login.py -v               # 详细日志
```

### 3. 掉线自动重连（定时任务）

**Linux / NAS / 飞牛 OS**（crontab，每 5 分钟检测，掉线自动重连）：

```bash
crontab -e
# 添加:
*/5 * * * * /usr/bin/python3 /path/to/njupt_login.py -c /path/to/config.ini >> /var/log/njupt_login.log 2>&1
```

**群晖 NAS**：使用「任务计划」→ 创建 → 计划任务 → 用户定义脚本，设置每 5 分钟运行：

```bash
python3 /volume1/scripts/njupt_login.py -c /volume1/scripts/config.ini
```

**Windows**（任务计划程序）：

```powershell
# 每 5 分钟运行一次
schtasks /create /tn "NJUPT Auto Login" /tr "python C:\scripts\njupt_login.py -c C:\scripts\config.ini" /sc minute /mo 5
```

## 配置说明

配置优先级（从高到低）：**命令行参数 > 环境变量 > 配置文件 > 默认值**

| 命令行参数 | 环境变量 | 配置文件 | 说明 |
|-----------|---------|---------|------|
| `-u/--username` | `NJUPT_USERNAME` | `[auth] username` | 学号/工号 |
| `-p/--password` | `NJUPT_PASSWORD` | `[auth] password` | 校园网密码 |
| `-o/--operator` | `NJUPT_OPERATOR` | `[auth] operator` | 运营商 |
| `--server` | `NJUPT_SERVER` | `[server] host` | 认证服务器 |
| `--port` | - | `[server] port` | 端口 |
| `--ip` | `NJUPT_IP` | `[server] ip` | 手动指定 IP |

### 运营商对照

| 参数值 | 运营商 | 账号格式 |
|-------|--------|---------|
| `cmcc` | 中国移动 | `,0,学号@cmcc` |
| `telecom` | 中国电信 | `,0,学号@telecom` |
| `default` | 校园网默认 | `,0,学号` |

## 认证原理

本脚本基于南邮校园网 Dr.COM（城市热点）eportal 门户认证协议，完整流程：

1. **获取客户端 IP**：访问认证页面解析 `v46ip`，或 socket 获取本机内网 IP
2. **加载页面配置**：调用 `/eportal/portal/page/loadConfig`（IP 需 base64 编码），获取 `program_index` / `page_index`
3. **登录认证**：调用 `/eportal/portal/login`（IP 明文），账号格式 `,0,用户名@运营商后缀`
4. **结果判断**：解析 JSONP 响应，`result=1` 为成功

成功响应示例：`dr1003({"result":1,"msg":"Portal协议认证成功！"})`

## 安全提示

- 建议设置文件权限：`chmod 600 config.ini`（Linux/NAS）
- 该认证协议密码为明文传输（协议本身决定），请在可信校园网环境使用
- 切勿将账号密码提交到任何公开仓库

## 使用示例

```
$ python3 njupt_login.py
[2026-10-31 11:21:38] [INFO ] NJUPT 校园网自动登录脚本 v1.0.0
[2026-10-31 11:21:38] [INFO ] 服务器: p.njupt.edu.cn:804 | 运营商: cmcc
[2026-10-31 11:21:38] [INFO ] 正在获取客户端 IP ...
[2026-10-31 11:21:38] [ OK  ] 客户端 IP: 10.161.240.179
[2026-10-31 11:21:39] [INFO ] 检测当前在线状态 ...
[2026-10-31 11:21:39] [INFO ] 当前未在线: 获取用户在线信息数据为空！
[2026-10-31 11:21:39] [INFO ] 加载认证页面配置 ...
[2026-10-31 11:21:39] [ OK  ] 页面配置加载成功
[2026-10-31 11:21:39] [INFO ] 正在以账号 B26011221@cmcc 登录 ...
[2026-10-31 11:21:40] [ OK  ] 登录成功! Portal协议认证成功！
[2026-10-31 11:21:41] [INFO ] 验证网络连通性 ...
[2026-10-31 11:21:42] [ OK  ] 网络连通性验证通过, 已成功联网!
```

## 免责声明

本项目仅供学习交流与个人合法使用，使用者需遵守所在学校的相关网络管理规定。因使用本脚本产生的任何问题与作者无关。

## 许可证

[MIT License](LICENSE)
