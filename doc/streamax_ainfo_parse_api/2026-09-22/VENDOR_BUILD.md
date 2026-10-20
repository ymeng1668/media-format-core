# streamax_video_aiinfo_parse 构建说明

预编译依赖分两处存放，**入库 + Git LFS**。普通开发者 clone 后可直接编译。

## 团队 / 客户

```bash
git clone <repo>
git lfs pull
build_windows.bat x64_msvc_2026 release   # Windows（兼容 x64 release）
./build.sh                               # Linux / Windows 交互选 platform key
```

无需运行 sync 脚本，无需主仓 sibling 路径。

## 维护者（更新依赖版本）

```bash
# 从 opencv453_lib git 刷新 opencv453（SDK）与 cryptopp/ffmpeg（Demo）
python scripts/streamax_deps.py sync --platform windows --arch all

# 从 monorepo 构建产物刷新 rmai_info_parse_api
python scripts/streamax_deps.py sync-rmai --platform windows --arch all --monorepo-root ..

# 自行 git add rmai_info_parse_api/ third_party/ 并 commit（二进制走 LFS）
```

Linux 若 `opencv453_lib` 无 ffmpeg/cryptopp，sync 会从 `--monorepo-root` 补充后再提交。

## 目录结构

```
rmai_info_parse_api/              # 客户 SDK（可单独 zip 交付）
  x64_msvc_2026/
    include/ lib/ bin/ opencv453/
  x86_msvc_2026/
  x86_64_gcc_13_3_0/
    include/ lib/ opencv453/

third_party/                      # Demo 工程自用依赖
  x64_msvc_2026/
    cryptopp/ ffmpeg/
  x86_msvc_2026/
  x86_64_gcc_13_3_0/
```

运行时依赖部署到 `release/bin/{platform}/`，按 SDK / Demo 分目录：

```
release/bin/linux/   或   release/bin/x64/
  streamax_video_aiinfo_parse(.exe)
  rmai_info_parse_api/    # SDK 运行时 SO/DLL（含 lib/ 与 opencv453）
  third_party_lib/        # Demo 运行时 SO/DLL（ffmpeg 等）

批量跑 demo（在 `release/bin` 下，需有 `ADAS_video` / `DSC_video` 目录）：

```bat
run_dmoe_batch_windows.bat x64
run_dmoe_batch_linux.sh
```
```

## 脚本子命令

| 命令 | 说明 | 使用者 |
|------|------|--------|
| `check` | 校验 SDK 与 Demo 依赖标记文件存在 | 构建脚本自动调用 |
| `sync` | clone opencv453_lib 并刷新 SDK + Demo 依赖 | 维护者 |
| `sync-rmai` | 更新 `rmai_info_parse_api/{key}/` | 维护者 |
| `deploy` | 部署运行时 DLL/SO 到 `release/bin/{platform}/rmai_info_parse_api/` 与 `third_party_lib/` | 构建后自动调用 |

## 环境要求

- Windows：VS2026 + Windows SDK 10.0.26100.0
- Linux：gcc 13.3 对应 `x86_64_gcc_13_3_0`（可通过 `RMAI_PRODUCT_TYPE` 覆盖）
