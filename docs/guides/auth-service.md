# 本地认证服务

认证服务把注册机已有的 SSO 会话转换为 CPA 可直接读取的 OAuth 凭据。注册继续在服务器运行，浏览器授权和凭据文件保存在本地。

## 配置远端同步

先把无密码导出器放到服务器项目目录：

```bash
scp scripts/export_registered_sessions.py user@server.example:/opt/grok-free-register/scripts/
```

在本地终端设置连接信息：

```bash
export XAI_AUTH_SERVICE_SSH_HOST=user@server.example
export XAI_AUTH_SERVICE_SSH_IDENTITY=/path/to/key.pem
export XAI_AUTH_SERVICE_REMOTE_ROOT=/opt/grok-free-register
```

使用 `ssh-agent` 时可省略 `XAI_AUTH_SERVICE_SSH_IDENTITY`。

## 运行

```bash
bash auth-service.sh
```

首次运行会自动安装项目依赖。该命令在当前终端持续运行并直接接受控制命令；输入 `q` 或按 `Ctrl-C` 停止，再次执行同一命令即可重启。不需要额外的会话管理工具。

普通模式只在来源连接、发现新账号、任务开始、认证结果、限流和控制状态变化时输出。查看队列、重试、节拍和冷却探针时使用：

```bash
bash auth-service.sh --debug
```

运行中终端底部会保持 `认证> ` 输入行。日志更新不会清掉尚未提交的内容；直接输入命令并回车：

```text
s       查看状态
take N  取用 N 个凭据
p       暂停
r       恢复
c       取消当前任务
q       安全退出
```

本地快照默认每 30 秒更新一次；内容无变化时终端保持安静。有效快照和已生成凭据会在重启后继续使用。
