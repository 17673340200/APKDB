# 多媒体播放器（PPT / PDF / 视频）

基于 Python + PyQt6 的桌面播放软件，一个窗口搞定三种素材的播放。

## 功能

- **PDF**：逐页渲染，打开即全屏放映，页面居中按整页可见显示（宽屏内容会自动铺满）
- **PDF 自动放映**：按间隔自动翻页，末页回到首页循环放映（可关闭循环），间隔秒数在工具栏设置
- **PPT / PPTX / PPS / ODP**：自动转成 PDF 后按页放映（转换结果缓存，第二次打开秒开）
- **视频**：播放 / 暂停、进度拖动、音量、快进快退、循环播放
- **网络流**：直接播放 RTSP / RTMP / HTTP 等流地址（`Ctrl+U` 或"文件 → 打开网络流"）
- **全屏播放**与完整快捷键（见下）
- **重启软件**：工具栏"重启软件"按钮会重新拉起程序并自动恢复上次打开的文件
- **启动即播**：未指定文件时，自动播放接收目录里最近收到的那个文件
- **定时播放**：「工具 → 定时播放计划」配置播放时段（开始时间 → 结束时间 + 内容），
  进入时段自动开始播放（可循环），过了结束时间自动暂停并保留当前画面（不会变黑屏）；
  服务端也可以远程下发计划（写入 `schedule.json`，重启后仍生效）
- **同步播放**：由服务端下发统一的起播时刻，多台机器同时开始播放同一内容，
  播放过程中还会按服务端时钟定期校正进度

## 安装

界面用的 Qt5（`PyQt5==5.15.11`），因此 **Windows 7（32/64 位）到 Windows 11 都能跑**：

```powershell
cd MediaPlayer
py -3 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

> 要在 **Windows 7** 或 **32 位** 机器上跑，请用 **32 位 Python 3.9** 建环境并打包：
> Python 3.10 起官方不再支持 Windows 7，PyQt6 也没有 32 位包，所以整套按 PyQt5 编写。
> 本项目里已准备了一个 32 位环境：`.venv32`（基于 `C:\Python39-32`）。

播放 PPT 需要一个"演示文稿转 PDF"的工具，本机装任意一个即可（仅 PPT 需要，PDF 和视频不需要）：

1. [LibreOffice](https://www.libreoffice.org/download/download/)（推荐，跨平台）；
2. Microsoft PowerPoint 或 WPS 演示（Windows，自动通过 COM 调用其导出 PDF，无需额外 Python 包）。

都没装的时候会弹出明确的提示。

## 运行

```powershell
.\.venv\Scripts\python main.py
```

## 快捷键

| 快捷键 | 作用 |
| --- | --- |
| `Ctrl+O` | 打开文件 |
| `Ctrl+U` | 打开网络流地址 |
| `F11` | 全屏 / 退出全屏 |
| `Esc` | 退出全屏 |
| `空格` / `→` / `↓` / `PageDown` | 下一页 |
| `←` / `↑` / `PageUp` / `Backspace` | 上一页 |
| `Home` / `End` | 首页 / 末页 |
| `A` | PDF 自动放映开关（间隔在工具栏设置） |
| PDF：`L` | 放映到末页后循环开关 |
| `Ctrl+滚轮`、`Ctrl` `+` / `-` | 缩放（`Ctrl+0` 重置） |
| 视频：`空格` | 播放 / 暂停 |
| 视频：`←` / `→` | 快退 / 快进 5 秒 |
| 视频：`↑` / `↓` | 音量 |
| 视频：`M` | 静音 |
| 视频：`L` | 循环播放开关 |

## 文件收发接口（TCP）

播放器启动时会在后台线程开启一个 TCP 服务（默认监听 `0.0.0.0:50517`，可用环境变量
`MEDIAPLAYER_FILE_PORT` 改端口），收到的文件保存在 `MediaPlayer/received/`，
上传完成后播放器会自动打开播放。

协议帧格式（请求与响应一致）：

```
| 4 字节 头部长度（大端 uint32） | 头部 JSON（UTF-8） | 数据体（可选） |
```

| 命令 | 头部字段 | 数据体 | 说明 |
| --- | --- | --- | --- |
| `ping` | — | — | 心跳 |
| `push` | `name`、`size` | 文件内容 | 上传文件，收完自动播放 |
| `play` | `name` | — | 播放已上传的文件 |
| `list` | — | — | 响应数据体为文件列表 JSON 数组 |
| `get` | `name` | — | 响应数据体为文件内容（下载） |

响应头部固定包含 `ok`、`cmd`、`msg`，失败时 `ok=false`。连接是长连接，可连续发多条命令。

配套的命令行客户端（纯标准库，可放在另一台机器上）：

```powershell
python file_client.py ping --host 192.168.1.10
python file_client.py list --host 192.168.1.10
python file_client.py push D:\media\demo.mp4 --host 192.168.1.10
python file_client.py get demo.mp4 D:\media\copy.mp4 --host 192.168.1.10
python file_client.py play demo.mp4 --host 192.168.1.10
```

代码里直接用 `FileClient("192.168.1.10").push("demo.mp4")` 也可以。

> 该接口没有鉴权，建议在可信的局域网内使用。

## 远程控制服务端（control_server.py）

`control_server.py` 是独立运行的控制端：播放器作为客户端主动连上来，服务端可以
远程操作播放器、推送文件、并让播放器把窗口画面实时回传（录屏）。

客户端接入有两种方式，二选一即可：

1. **服务端添加客户端**（推荐，播放器端无需配置）：播放器启动后会自动监听
   `50519` 端口（`MEDIAPLAYER_LISTEN_PORT` 可改），服务端填 IP 就能连上来。
2. **播放器主动连出**：设置环境变量后启动播放器。

```powershell
$env:MEDIAPLAYER_CONTROL_HOST = "192.168.1.10"   # 控制服务端 IP
$env:MEDIAPLAYER_CONTROL_PORT = "50518"          # 可选，默认 50518
python main.py
```

添加客户端：图形界面点「添加客户端」填名称 / IP / 端口（默认 50519），
或命令行 `add 192.168.1.20 50519 前台屏`；地址簿保存在 `clients.json`（`--book` 可改），
服务端重启后仍在，对离线的客户端会每 5 秒自动重连。`remove <IP> [端口]` 或
界面「移除选中」可删除，`book` 查看地址簿。

服务端启动与界面操作：

```powershell
python control_server.py                      # 图形界面（默认）
python control_server.py --cli                # 命令行界面
python control_server.py --port 50518 --shots D:\shots
```

图形界面：左侧「添加客户端」登记 IP，列表显示在线/离线状态，右侧分区控制——播放/暂停/停止、快进快退、音量、静音/循环/全屏、
文档翻页与自动放映（间隔、末页循环、适应宽度）、推送文件与流地址、**客户端文件**
（列出客户端接收目录里的文件，选中即让客户端播放；"删除选中"会连同客户端本机的文件一起删除并自动刷新列表）、**实时画面**（选帧率与宽度，
客户端持续回传窗口画面，界面内实时预览；勾"录制到文件夹（MP4 视频）"即把实时画面录成
`record.mp4` 放到 `saves/client编号_时间戳/` 里（"打开目录"就是打开这个录制文件夹），
也可"保存当前画面"为单张 JPG），底部是实时日志。**录制顺序：先点"开始实时画面"，再勾选录制**；
取消勾选或点"停止"即生成视频（没收到画面帧时不会留下空文件夹）。画面按窗口在屏幕上的位置截取，
所以**视频播放内容本身**也会出现在实时画面和录像里。录制需要 ffmpeg：`python -m pip install imageio-ffmpeg`；
勾上**"录制系统声音"**还能把客户端正在播放的声音一起录进 MP4（客户端需装 `python -m pip install soundcard`，
走 Windows WASAPI 回环采集；没装或没声卡时只会提示，录像仍正常出片）。
（用 `.venv` 启动时要用 `.\.venv\Scripts\python -m pip install imageio-ffmpeg`，装到**启动服务端那个解释器**里才生效）。

**批量 / 同步 / 定时**分组（客户端列表可按住 Ctrl/Shift 多选，也可"全选在线"）：
- `推送文件给选中…`：把同一个文件并发推给所有选中的客户端（各端收到即播放）；
- `同步播放`：内容填本地文件则先批量推送，然后下发统一的起播时刻（可设几秒后起播）；
  也可以直接填客户端接收目录里已有的文件名，让各端同时开始播放；
- `加入定时计划`：配置播放时段（开始 / 结束时间、内容、是否循环）后下发给选中的客户端，
  计划列表可查看当前配置并删除某一项；`清空并下发` 清空客户端的计划。
  客户端每 15 秒检查一次：进入时段自动开始播放，过了结束时间自动暂停并保留当前画面。
  结束时间留空（取消勾选"到时停止"）表示从开始时间一直播到当天结束；支持跨零点时段如 22:00 → 06:00。
  同步播放依赖各端系统时间接近，客户端会用服务端下发的时间戳做偏移校正。

命令行可用指令：

```powershell
clients            # 列出客户端
add <IP> [端口] [名称]   # 添加客户端并连接（默认端口 50519）
remove <IP> [端口]      # 移除客户端
book               # 显示地址簿
use 1              # 选择客户端
push D:\media\a.mp4 [all|1,2]   # 推送文件（all=所有在线客户端，1,2=指定编号），收到后自动播放
sync D:\media\a.mp4 3 all    # 先批量推送，再让所有客户端 3 秒后同步起播
sync a.mp4 3 all             # 各端接收目录里已有的文件，直接同步起播
schedule add 08:30 18:00 a.mp4 all   # 每天 08:30 播到 18:00（到 18:00 自动停止）
schedule add 22:00 06:00 a.mp4 all   # 跨零点时段
schedule add 08:30 - a.mp4 all       # 只给开始时间：从 08:30 一直播到当天结束
schedule off all             # 清空定时计划
open rtsp://...              # 让客户端打开本地文件或网络流
files                        # 列出客户端接收目录里的文件
play 文档.pdf                # 让客户端播放接收目录里的指定文件
delete 文档.pdf              # 删除客户端接收目录里的文件（客户端本机同步删除）
play / pause / stop / seek 5000 / volume 30 / mute on / loop on
full on|off / next / prev / page 3 / autoplay on / interval 5
record 8 [宽度]    # 客户端实时画面（录屏），默认 8 fps / 宽 960，off 停止
savevideo on|off   # 把实时画面录成 MP4（存到录制文件夹）
shot               # 单张截屏并回传（默认存到与 received 同级的 saves 目录，--shots 可改）
autoshot 5         # 客户端每 5 秒自动截屏回传（off 关闭）
restart / quit     # 重启 / 退出客户端软件
```

控制帧同样使用「头部长度 + JSON + 数据体」格式，命令有 `hello/welcome`、
`control`（action + args）、`push`、`shot`、`autoshot`、`record`、`frame`、`event`、`shot_data`、`ping`。
断线后播放器每 5 秒自动重连；该通道同样没有鉴权，请在可信局域网使用。

## 目录结构

```
main.py                 启动入口
core/document.py        PDF 文档模型与页面渲染（PyMuPDF）
core/converter.py       PPT → PDF 转换与缓存（LibreOffice / PowerPoint / WPS）
core/file_protocol.py   文件收发协议（帧编解码，纯标准库）
core/file_server.py     文件收发服务端（QThread 后台运行）
core/control_protocol.py 远程控制协议常量
core/control_client.py  播放器侧受控客户端（连控制服务端）
core/audio_capture.py   采集本机播放的系统声音（WASAPI 回环 → PCM，录制带声音时需要）
schedule.json           定时播放计划（服务端下发 / 客户端编辑后写入）
control_server.py       远程控制服务端（默认图形界面，--cli 走命令行）
control_server_ui.py    控制服务端图形界面（PyQt6）
file_client.py          文件收发命令行客户端
ui/document_view.py     PDF/幻灯片视图（翻页、缩放）
ui/video_view.py        视频播放视图（QMediaPlayer）
ui/main_window.py       主窗口、菜单、工具栏、全屏与快捷键
build.py                打包脚本（PyInstaller，分别产出客户端 / 服务端）
```

## 打包成 exe（客户端 / 服务端分开）

用 PyInstaller 分别打包，产物在 `dist/`（`--onedir`，绿色版，整个目录拷走即可）：

```powershell
python build.py           # 两个都打包
python build.py client    # 只打包客户端
python build.py server    # 只打包服务端
```

产物：

| 目录 | 可执行文件 | 说明 |
| --- | --- | --- |
| `dist/MediaPlayer客户端/` | `MediaPlayer客户端.exe` | 播放器（图形界面），拷到播放机上直接双击 |
| `dist/MediaPlayer服务端/` | `MediaPlayer服务端.exe` | 控制端图形界面 |
| `dist/MediaPlayer服务端命令行/` | `MediaPlayer服务端命令行.exe` | 控制端命令行（`--help` 看参数，`--cli` 进交互模式） |

须知：
- **要出能在 Windows 7 / 32 位上跑的 exe，必须用 32 位 Python 3.9 打包**（本机即 `.venv32`）：

  ```powershell
  .\.venv32\Scripts\python build.py
  ```

  这样产出的三个 exe 都是 32 位（PE32），32 位 / 64 位 Windows 都能运行。
  用 64 位解释器打包则只能在 64 位 Windows 上跑。
- 用哪个解释器打包，就打包那个解释器里装好的依赖；缺包请先在对应环境装好
  （客户端需要 `soundcard` 才能录系统声音，服务端需要 `imageio-ffmpeg` 提供 ffmpeg）。
- 服务端目录里已自带一份 ffmpeg（32 位环境打包时是 `ffmpeg-win32-v4.2.2.exe`）：
  录屏时优先用它，找不到时才退回系统 PATH；也可以删掉它改用自装的 ffmpeg。
- 程序运行时会在 exe 所在目录创建 `received/`（收到的文件）、`saves/`（截屏与录像）、
  `schedule.json`（定时计划），所以目录要可写。
- 客户端打包时排除了 `pymupdf.layout`（PDF 布局分析，可选功能）：
  它在导入时要读自带的资源文件，打不进包会让客户端启动就崩；我们只用它的渲染，排掉无影响。

### 视频播放的注意点（Qt5）

Qt5 的 `QMediaPlayer` 走系统解码器（Windows 上为 WMF / DirectShow），**不自带解码器**：

- 在 Windows 7 / 10 上一般能直接播 MP4（H.264）、WMV、MP3 等常见格式；
- 如果目标机是精简版系统或缺少"媒体功能包"，可能出现"打不开/无画面"，
  装上系统解码器（或改用 PyQt6 版本，Qt6 自带 FFmpeg 后端）即可；
- 打包时 `PyQt5` 的 `mediaservice` 插件（wmfengine / dsengine）会一起打进去，无需额外配置。
