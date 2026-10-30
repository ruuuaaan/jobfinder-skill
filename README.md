# JobFinder Skill

跨 Agent 的中国大陆岗位检索技能，版本 0.3.0-beta.1 / Beta / MIT。
完整复制本仓库的 jobfinder/ 到宿主认可的技能目录。支持读取 SKILL.md 并提供文件、终端工具的 agent；实时搜索还需要宿主联网与浏览器能力。不依赖 Codex，也不附带模型 API。

详细安装、命令和工具能力映射见 [跨 Agent 指南](jobfinder/references/host-compatibility.md)。
依赖 Python 3.11+；使用虚拟环境安装 jobfinder/requirements.txt。Skill 代码、地理数据和模板均随包携带，用户数据写入独立 WORKSPACE。

首次试用：运行 jobfinder/scripts/run.py --workspace <实际目录> init，再按指南执行离线样例。它会生成明确标注为合成数据的十列 Excel；不代表完成了实时招聘搜索。

通用调用与隔离工作目录经过自动化验证；未逐一安装测试所有第三方 agent 的技能发现与浏览器接口。纯聊天工具无法执行；没有搜索能力时只能处理已提供材料。

独立网页后台请选择另一个 Agent 版本。此目录可作为全新 Git 仓库上传；不要把个人工作目录合并进来。修改后用 scripts/smoke_release.py 检查，发布前重新生成清单。

数据说明见 [DATA_SCHEMA.md](DATA_SCHEMA.md)，许可见 [LICENSE](LICENSE)，数据出处见 [第三方说明](THIRD_PARTY_NOTICES.md)。
