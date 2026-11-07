# 一键把本项目推送到 GitHub（之后用 Actions 云端构建安卓 APK）。
#
# 用法：在 MediaPlayer 目录里打开 PowerShell，执行
#   powershell -ExecutionPolicy Bypass -File .\push_to_github.ps1
# 或指定仓库地址：
#   powershell -ExecutionPolicy Bypass -File .\push_to_github.ps1 -RepoUrl https://github.com/<用户名>/<仓库名>.git
#
# 前置条件：
#   1. 有 GitHub 账号，并已在网页上新建一个【空仓库】（New repository，不要勾选 README/.gitignore/license）；
#   2. 本机能访问 github.com（若超时请先开代理再重跑）。
#
# 脚本会自动：
#   - 没装 Git 就装便携版（免管理员，走清华镜像）到 %LOCALAPPDATA%\PortableGit
#   - git init + 按项目里的 .gitignore 提交源码（几百 MB 的 dist/venv 不会推上去）
#   - 添加远程并推送（首次推送 Windows 会弹 GitHub 登录窗口，按提示授权即可）

param([string]$RepoUrl = "")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$GitDir = "$env:LOCALAPPDATA\PortableGit"
$Git = "$GitDir\cmd\git.exe"

# ---------- 1. 确保 git 可用 ----------
if (-not (Test-Path $Git)) {
    $Sfx = "$env:TEMP\PortableGit-2.56.0.4-64-bit.7z.exe"
    if (-not (Test-Path $Sfx)) {
        Write-Host "下载便携版 Git（清华镜像，约 60MB）..."
        Invoke-WebRequest `
            -Uri "https://mirrors.tuna.tsinghua.edu.cn/github-release/git-for-windows/git/LatestRelease/PortableGit-2.56.0.4-64-bit.7z.exe" `
            -OutFile $Sfx -UseBasicParsing
    }
    Write-Host "解压便携版 Git ..."
    & $Sfx -o $GitDir -y | Out-Null
}
& $Git --version

# ---------- 2. 本地仓库 ----------
Set-Location $Root
if (-not (Test-Path "$Root\.git")) {
    & $Git init | Out-Null
}
$name = & $Git config user.name
if (-not $name) {
    $name = Read-Host "提交用的名字（GitHub 用户名即可，回车默认 Administrator）"
    if (-not $name) { $name = "Administrator" }
    & $Git config user.name $name
}
$email = & $Git config user.email
if (-not $email) {
    $email = Read-Host "提交用的邮箱（回车默认占位邮箱）"
    if (-not $email) { $email = "$name@users.noreply.github.com" }
    & $Git config user.email $email
}

& $Git add -A
$needCommit = (& $Git status --porcelain) -ne $null
if ($needCommit) {
    & $Git commit -m "多媒体播放器：桌面版(PyQt6) + Win7版(Qt5) + 安卓版(PySide6)"
} else {
    Write-Host "没有新改动，跳过提交"
}

# ---------- 3. 推送 ----------
if (-not $RepoUrl) {
    $RepoUrl = Read-Host "GitHub 仓库地址（https://github.com/<用户名>/<仓库名>.git）"
}
if (-not $RepoUrl) { Write-Host "未提供仓库地址，已停止（本地提交已完成）"; exit 1 }

& $Git remote remove origin 2>$null
& $Git remote add origin $RepoUrl
& $Git branch -M main
Write-Host "推送中（首次会弹 GitHub 登录窗口，请完成授权）..."
& $Git push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "推送失败：多为网络不通 github.com（先开代理再重跑本脚本）或仓库地址/权限有误" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "推送完成！接下来出 APK：" -ForegroundColor Green
Write-Host "  1. 打开仓库网页 → Actions 标签 → 左侧选『Android APK』→ Run workflow → 选 main 分支 → Run"
Write-Host "  2. 等构建结束（首次约 20-40 分钟，之后有缓存会快很多）"
Write-Host "  3. 点进那次运行 → 页面底部 Artifacts → 下载 MediaPlayer-android-apk（zip 里就是 apk）"
Write-Host "  4. 手机上直接安装该 apk（arm64，Android 8+）"
