# Nexus Cleanup

自动清理 Nexus Repository 中的旧 SNAPSHOT 版本，保留最新的若干个构建。

## 特性

- 按 artifact 分组，保留指定数量的最新 SNAPSHOT
- 支持并发删除，加快清理速度
- 内置 healthcheck 端点，方便容器编排监控
- 支持定时执行或单次手动运行
- 提供 dry-run 模式，预览即将删除的组件
- 多架构镜像支持（linux/amd64、linux/arm64）

## 快速开始

```bash
docker-compose up -d
```

或直接使用镜像：

```bash
docker run -d \
  -e NEXUS_URL=http://nexus:8081 \
  -e NEXUS_USER=admin \
  -e NEXUS_PASS=admin123 \
  -e REPOSITORY_NAME=maven-snapshots \
  -p 8000:8000 \
  ghcr.io/freemankevin/nexus-cleanup:latest
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `NEXUS_URL` | Nexus 服务地址 | `http://nexus:8081` |
| `NEXUS_USER` | Nexus 用户名 | `admin` |
| `NEXUS_PASS` | Nexus 密码 | `admin123` |
| `REPOSITORY_NAME` | 目标仓库名 | `maven-snapshots` |
| `RETAIN_COUNT` | 每个 artifact 保留的版本数 | `3` |
| `DRY_RUN` | 仅预览不删除 (`true`/`false`) | `false` |
| `SCHEDULE_TIME` | 每日执行时间，`manual` 表示立即执行一次 | `03:00` |
| `DELETE_WORKERS` | 并发删除线程数 | `5` |
| `LOG_LEVEL` | 日志级别 | `INFO` |

## 镜像

- **Registry**: `ghcr.io/freemankevin/nexus-cleanup`
- **Tags**: `latest`、`sha-<commit>`、`v*`
- **Platforms**: `linux/amd64`、`linux/arm64`

## 健康检查

容器暴露 `8000` 端口提供 HTTP 健康检查：

```bash
curl http://localhost:8000/health
```

返回包含存储、Nexus 连通性和最近一次清理状态的综合检查结果。
