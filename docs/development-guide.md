# StAgent 项目开发文�?

## 项目概述

StAgent 是一个开源的桌面 AI 代理平台，专注于实际工作执行而非简单对话。它融合了三个核心能力：流式代理运行时、代理视觉工作流和生成式 UI 层，提供 100+ 内置离线工具的完整自托管解决方案�?

### 核心特�?

- **代理运行�?*：多轮会话、令牌流式传输、工具执行、分层模型路由、认知三重记�?
- **100+ 离线工具**：文档、网页、数据、代码、数据库、媒体、游戏艺术生�?
- **视觉工作�?*：ReactFlow 编辑器、YAML 导出、可重用模板、自动类型化节点
- **代理技�?*：Agent Skills v1.0 规范、渐进式披露、按需加载
- **研究论文模式**：PDF 结构提取、引用摘要、LaTeX 公式提取、区域翻�?
- **生成�?UI**：KPI 仪表板、幻灯片、画廊等交互界面
- **多提供商 LLM**：DeepSeek、DashScope、OpenAI、Anthropic、Azure OpenAI、Ollama、vLLM
- **零配置默�?*：SQLite 开箱即用，�?Docker 容器部署

## 技术架�?

### 系统分层

```
┌─────────────────────────────────────────────────────────────�?
�?                    入口�?(Ingress)                          �?
�? HTTP/SSE/WebSocket/Task/Cron/CLI/Desktop                    �?
└─────────────────────────────────────────────────────────────�?
                              �?
                              �?
┌─────────────────────────────────────────────────────────────�?
�?                   门面�?(Facade)                            �?
�? ServiceManager | AgentRuntime | WorkflowService             �?
└─────────────────────────────────────────────────────────────�?
                              �?
                              �?
┌─────────────────────────────────────────────────────────────�?
�?                   核心�?(Kernel)                            �?
�? run_loop �?QueryEngine �?ToolExecutor                       �?
└─────────────────────────────────────────────────────────────�?
                              �?
                              �?
┌─────────────────────────────────────────────────────────────�?
�?                 持久化状态层 (Durable State)                  �?
�? SessionState | CheckpointStore | WorkflowStateStore         �?
└─────────────────────────────────────────────────────────────�?
                              �?
                              �?
┌─────────────────────────────────────────────────────────────�?
�?                 可观测性层 (Observability)                    �?
�? EventManager | OTel | Metrics | Logging                     �?
└─────────────────────────────────────────────────────────────�?
```

### 核心模块

#### 1. 代理运行�?(Agent Runtime)

**位置**: `backend/StAgent/runtime/`

**职责**: 统一的代理执�?SDK，提供声明式代理定义和执行门面�?

**核心组件**:

- **AgentDefinition**: 声明式代理定义，包含人设、工具策略、模型策略、记忆策�?
- **RuntimeContext**: 服务注入包，提供 LLM、工具注册表、执行器、记忆、会话管�?
- **AgentRuntime**: 执行门面，解析定义、物化配置、驱�?QueryEngine 循环

**使用示例**:

```python
from StAgent.runtime import AgentBuilder

support = (
    AgentBuilder("support_agent")
    .describe("Customer support specialist")
    .variant("default_agent")
    .tools(allow=["web_search", "knowledge_*"], max_tools=12)
    .model(task="chat", temperature=0.3)
    .memory(recall_limit=8)
    .runtime(profile="standard", max_turns=12)
    .build()
)

# 执行代理
runtime = AgentRuntime.from_service_manager(service_manager)
result = await runtime.run("support_agent", "Help with issue", session_id=sid)
```

#### 2. 执行拓扑 (Execution Topology)

**核心原则**: 所有代理调用必须通过 `StAgent.sdk.kernel.loop.run_loop`�?

**执行路径**:

| 调用�?| 入口�?| 核心循环 |
|--------|--------|----------|
| 聊天 SSE | `AgentController.run_stream` | `run_loop` |
| SDK | `AgentRuntime.stream` | `run_loop` |
| 后台任务 | `AgentTaskHandler.spawn` | `AgentRuntime.stream` �?`run_loop` |
| 子代�?| `subagent._run_engine` | `run_loop` |
| 工作流代理节�?| `agent_exec.run_agent_node` | `AgentRuntime.stream` �?`run_loop` |

#### 3. 工作流引�?(Workflow Engine)

**位置**: `backend/StAgent/workflow/`

**支持模型**:

- **DAG 引擎**: 保存的工作流、定时任务、代�?`workflow_run` 工具
- **聊天步骤卡片**: 编译为线性工作流文档的步骤执�?
- **聊天嵌入**: 验证�?Flow JSON 预览和执�?

**特�?*:
- ReactFlow 编辑器集�?
- YAML 导出/导入
- 可重用模�?
- 自动类型化节�?
- 批处理并发执�?
- 集中重试/退避和超时

#### 4. 工具系统 (Tool System)

**位置**: `backend/StAgent/tools/`

**工具类别**:

- **文档工具**: PDF 读取、OCR、文档生成、格式转�?
- **网页工具**: 搜索、抓取、浏览器自动�?
- **数据工具**: CSV/Excel 处理、数据库查询、数据分�?
- **代码工具**: 代码执行、项目分析、重�?
- **媒体工具**: 图像生成、视频处理�?D 网格
- **生成�?UI**: KPI 仪表板、幻灯片、画�?

**工具注册**:

```python
from StAgent.tools import ToolRegistry

registry = ToolRegistry()
registry.register_tool("web_search", WebSearchTool)
registry.register_tool("knowledge_query", KnowledgeQueryTool)
```

#### 5. 记忆系统 (Memory System)

**三重记忆架构**:

- **情景记忆**: 会话历史和事件记�?
- **语义记忆**: 知识库和向量索引
- **程序记忆**: 技能、规则和流程

**配置策略**:

```python
from StAgent.runtime import MemoryPolicy

memory = MemoryPolicy(
    enabled=True,
    recall_limit=6,
    formation=True  # 控制记忆写入
)
```

#### 6. 安全控制平面 (Security Control Plane)

**认证机制**:

- **实例访问密码**: PBKDF2 哈希存储�?`$LEAGENT_HOME/security.json`
- **会话 JWT**: 通过 `StAgent_SECRET_KEY` HMAC 签名
- **多用户支�?*: 命名用户表和权限管理

**安全策略**:

- 强制认证: `StAgent_SECURITY_ENFORCE_AUTH`
- 速率限制: 自动启用认证�?
- OpenAPI 文档保护: 认证时禁�?
- 工作流所有权验证

## 开发环境搭�?

### 前置要求

- Python 3.11+
- Node.js 18+
- uv (Python 包管理器)
- Git

### 后端开发环�?

```bash
# 进入后端目录
cd backend

# 使用 uv 同步依赖
uv sync

# 激活虚拟环�?
uv shell

# 运行测试
uv run python -m pytest tests/ -v

# 启动开发服务器
uv run uvicorn StAgent.main:app --reload --host 0.0.0.0 --port 8000
```

### 前端开发环�?

```bash
# 进入前端目录
cd frontend

# 安装依赖
npm install

# 启动开发服务器
npm run dev

# 构建生产版本
npm run build
```

### 桌面应用开�?

```bash
# 进入桌面应用目录
cd desktop

# 安装依赖
npm install

# 启动开发模�?
npm run dev

# 构建应用
npm run build
```

### 数据库配�?

**开发环�?(SQLite)**: 零配置，自动使用

**生产环境 (PostgreSQL)**:

```bash
# 安装 PostgreSQL 驱动
cd backend
uv sync --extra postgres

# 配置环境变量
export DATABASE_URL="postgresql+asyncpg://user:password@localhost/StAgent"
```

### 环境变量

**核心环境变量**:

```bash
# 应用配置
StAgent_HOME=/path/to/StAgent/home
StAgent_SECRET_KEY=your-secret-key
StAgent_DEBUG=true

# 模型配置
DEEPSEEK_API_KEY=your-deepseek-key
OPENAI_API_KEY=your-openai-key

# 安全配置
StAgent_SECURITY_ENFORCE_AUTH=true
StAgent_SECURITY_RATE_LIMIT_PER_MINUTE=60

# 数据库配�?
DATABASE_URL=sqlite+aiosqlite:///./StAgent.db

# 工作进程
LEAGENT_WORKERS=1  # SQLite 使用 1，PostgreSQL 可增�?
```

## 开发指�?

### 代码结构

```
backend/
├── StAgent/
�?  ├── runtime/          # 代理运行�?SDK
�?  ├── sdk/              # 核心 SDK 和内�?
�?  ├── tools/            # 工具实现
�?  ├── workflow/        # 工作流引�?
�?  ├── memory/           # 记忆系统
�?  ├── api/              # FastAPI 路由
�?  ├── models/           # 数据模型
�?  └── services/         # 业务服务
├── tests/                # 测试套件
└── pyproject.toml        # Python 项目配置

frontend/
├── src/
�?  ├── components/       # React 组件
�?  ├── pages/            # 页面组件
�?  ├── hooks/            # 自定�?Hooks
�?  ├── store/            # 状态管�?
�?  └── utils/            # 工具函数
└── package.json          # Node.js 项目配置

desktop/
├── src/
�?  ├── main/             # Electron 主进�?
�?  ├── renderer/         # 渲染进程
�?  └── preload/          # 预加载脚�?
└── package.json          # Electron 项目配置
```

### 添加新工�?

1. **创建工具�?*:

```python
# backend/StAgent/tools/my_tool.py
from StAgent.tools import Tool, ToolResult
from pydantic import BaseModel

class MyToolInput(BaseModel):
    query: str

class MyTool(Tool):
    name = "my_tool"
    description = "My custom tool description"
    
    async def execute(self, input: MyToolInput) -> ToolResult:
        # 实现工具逻辑
        result = f"Processed: {input.query}"
        return ToolResult.success(result)
```

2. **注册工具**:

```python
# backend/StAgent/tools/registry.py
from .my_tool import MyTool

def register_custom_tools(registry: ToolRegistry):
    registry.register_tool(MyTool)
```

3. **更新代理定义**:

```python
from StAgent.runtime import AgentBuilder

agent = (
    AgentBuilder("my_agent")
    .tools(allow=["my_tool", "web_search"])
    .build()
)
```

### 添加新代理技�?

1. **创建 SKILL.md 文件**:

```markdown
# My Custom Skill

## Description
Brief description of what this skill does.

## Usage
How to use this skill.

## Examples
Example usage scenarios.
```

2. **放置技能文�?*:

```bash
# 官方技�?
backend/StAgent/extensions/skills/official/my_skill/SKILL.md

# 用户技�?
$StAgent_HOME/skills/my_skill/SKILL.md
```

3. **技能自动发�?*:

代理运行时会自动发现和加�?SKILL.md 文件�?

### 添加工作流节�?

1. **定义节点类型**:

```python
# backend/StAgent/workflow/nodes/my_node.py
from StAgent.workflow import Node, NodeResult

class MyNode(Node):
    node_type = "my_node"
    description = "My custom workflow node"
    
    async def execute(self, context: NodeContext) -> NodeResult:
        # 实现节点逻辑
        return NodeResult.success(data={"output": "result"})
```

2. **注册节点**:

```python
# backend/StAgent/workflow/registry.py
from .nodes.my_node import MyNode

def register_custom_nodes(registry: NodeRegistry):
    registry.register_node(MyNode)
```

### 测试指南

**单元测试**:

```python
# tests/test_my_tool.py
import pytest
from StAgent.tools.my_tool import MyTool, MyToolInput

@pytest.mark.asyncio
async def test_my_tool():
    tool = MyTool()
    input_data = MyToolInput(query="test")
    result = await tool.execute(input_data)
    assert result.success
    assert "test" in result.data
```

**集成测试**:

```python
# tests/integration/test_agent_flow.py
import pytest
from StAgent.runtime import AgentRuntime

@pytest.mark.integration
async def test_agent_with_tool():
    runtime = AgentRuntime.from_service_manager(service_manager)
    result = await runtime.run("default_agent", "Use my_tool")
    assert result.success
```

**运行测试**:

```bash
# 所有测�?
uv run python -m pytest tests/ -v

# 集成测试
uv run python -m pytest tests/ -v -m integration

# 特定文件
uv run python -m pytest tests/test_my_tool.py -v
```

### 代码质量

**Linting**:

```bash
# 使用 ruff 进行代码检�?
uv run ruff check backend/StAgent/

# 自动修复
uv run ruff check --fix backend/StAgent/
```

**类型检�?*:

```bash
# 使用 mypy 进行类型检�?
uv run mypy backend/StAgent/
```

**格式�?*:

```bash
# 使用 ruff 进行格式�?
uv run ruff format backend/StAgent/
```

## API 文档

### 核心 API 端点

**认证**:

```
POST /api/v1/auth/setup      # 初始设置
POST /login                  # 登录
POST /logout                 # 登出
GET  /me                     # 当前用户信息
GET  /status                 # 系统状�?
```

**聊天**:

```
POST   /api/v1/chat/sessions           # 创建会话
GET    /api/v1/chat/sessions/{id}      # 获取会话
POST   /api/v1/chat/sessions/{id}/messages  # 发送消�?
GET    /api/v1/chat/sessions/{id}/stream    # 流式响应
POST   /api/v1/chat/sessions/{id}/resume-checkpoint  # 恢复检查点
```

**代理**:

```
GET    /api/v1/agents                  # 代理列表
GET    /api/v1/agents/{name}           # 代理详情
POST   /api/v1/agents/{name}/run       # 运行代理
```

**工作�?*:

```
GET    /api/v1/workflows/flows        # 工作流列�?
POST   /api/v1/workflows/flows        # 创建工作�?
GET    /api/v1/workflows/flows/{id}   # 工作流详�?
POST   /api/v1/workflows/flows/{id}/run  # 运行工作�?
GET    /api/v1/workflows/executions/{id}/stream  # 流式执行
```

**工具**:

```
GET    /api/v1/tools                   # 工具列表
GET    /api/v1/tools/{name}            # 工具详情
POST   /api/v1/tools/{name}/execute    # 执行工具
```

### OpenAPI 文档

启动服务后访�?
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## 部署指南

### Docker 部署

**构建镜像**:

```bash
docker build -t StAgent:latest .
```

**运行容器**:

```bash
docker run -d \
  -p 8000:8000 \
  -v StAgent_data:/app/data \
  -e StAgent_SECRET_KEY=your-secret-key \
  -e DEEPSEEK_API_KEY=your-api-key \
  StAgent:latest
```

### 生产环境配置

**数据库迁�?*:

```bash
cd backend
uv run alembic upgrade head
```

**进程管理**:

```bash
# 使用 gunicorn
uv run gunicorn StAgent.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000
```

**反向代理配置** (Nginx):

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

### 监控和日�?

**日志位置**:
- 开发环�? `logs/` 目录
- 生产环境: `$StAgent_HOME/logs/`

**指标端点**:
- Prometheus: `http://localhost:8000/metrics`
- 健康检�? `http://localhost:8000/health`

**调试模式**:

```bash
export StAgent_DEBUG=true
```

## 贡献指南

### 开发流�?

1. Fork 项目仓库
2. 创建功能分支: `git checkout -b feature/my-feature`
3. 提交更改: `git commit -am 'Add my feature'`
4. 推送分�? `git push origin feature/my-feature`
5. 创建 Pull Request

### 代码规范

- 遵循 PEP 8 代码风格
- 使用类型注解
- 编写单元测试
- 更新相关文档
- 通过 CI 检�?

### 提交信息

使用约定式提交格�?

```
feat: add new tool for data analysis
fix: resolve memory leak in agent runtime
docs: update API documentation
test: add integration tests for workflow engine
```

### 问题报告

报告问题时请包含:
- 环境信息 (OS, Python 版本)
- 复现步骤
- 预期行为
- 实际行为
- 错误日志

## 性能优化

### 数据库优�?

**SQLite**:
- 保持 `StAgent_WORKERS=1`
- 定期执行 `VACUUM`
- 使用 WAL 模式

**PostgreSQL**:
- 配置连接�?
- 创建适当索引
- 启用查询缓存

### 缓存策略

**内存缓存**:

```python
from StAgent.cache import CacheConfig

cache_config = CacheConfig(
    enabled=True,
    ttl_seconds=3600,
    max_size=1000
)
```

**Redis 缓存** (生产环境):

```bash
uv sync --extra production
export REDIS_URL=redis://localhost:6379
```

### 模型路由优化

**成本分层路由**:

```python
from StAgent.runtime import ModelPolicy

model = ModelPolicy(
    task="chat",
    provider="deepseek",  # 成本最�?
    temperature=0.7,
    max_output_tokens=4096
)
```

## 故障排查

### 常见问题

**代理执行失败**:
- 检�?LLM API 密钥配置
- 验证网络连接
- 查看日志中的错误信息

**工具执行超时**:
- 增加工具超时配置
- 检查系统资源使用情�?
- 优化工具实现

**数据库锁�?*:
- 确保使用正确的数据库配置
- 检查并发连接数
- 考虑升级�?PostgreSQL

**内存不足**:
- 减少 `recall_limit` 配置
- 禁用不必要的功能
- 增加系统内存

### 调试技�?

**启用详细日志**:

```bash
export StAgent_DEBUG=true
export StAgent_LOG_LEVEL=debug
```

**代理追踪**:

```python
from StAgent.runtime import AgentRuntime

runtime = AgentRuntime.from_service_manager(service_manager)
async for event in runtime.stream("default_agent", "test"):
    print(f"Event: {event.type}, Data: {event.data}")
```

**工作流调�?*:

```bash
# 导出工作�?YAML
POST /api/v1/workflows/flows/{id}/export
```

## 扩展开�?

### MCP 服务器集�?

**添加 MCP 服务�?*:

```python
# backend/StAgent/mcp/my_server.py
from StAgent.mcp import MCPServer

class MyMCPServer(MCPServer):
    server_name = "my_server"
    
    async def initialize(self):
        # 初始�?MCP 服务�?
        pass
```

### 自定义模型集�?

**添加新模型提供商**:

```python
# backend/StAgent/llm/my_provider.py
from StAgent.llm import LLMProvider

class MyLLMProvider(LLMProvider):
    provider_name = "my_provider"
    
    async def generate(self, prompt: str, **kwargs) -> str:
        # 实现模型调用逻辑
        pass
```

### 插件系统

**创建插件**:

```python
# backend/StAgent/plugins/my_plugin.py
from StAgent.plugins import Plugin

class MyPlugin(Plugin):
    plugin_name = "my_plugin"
    version = "1.0.0"
    
    def initialize(self, app):
        # 插件初始化逻辑
        pass
```

## 参考资�?

### 官方文档

- [项目 README](../../README.md)
- [中文文档](../../README_zh.md)
- [技术文档](./technical/)
- [用户指南](./guide/)
- [教程](./tutorial.md)

### 核心模块文档

- [代理运行�?SDK](./technical/agent-runtime.md)
- [执行拓扑](./technical/execution-topology.md)
- [安全控制平面](./technical/security-control-plane.md)
- [代理 SDK](./technical/agent_sdk.md)
- [工具参数](./technical/tool-parameters.md)

### 社区资源

- GitHub Issues: https://github.com/WeiY11/StAgent/issues
- Discussions: https://github.com/WeiY11/StAgent/discussions
- Agent Skills 规范: https://agentskills.my/specification

## 许可�?

Apache License 2.0 - 详见 [LICENSE](../../LICENSE) 文件

## 联系方式

- 作�? Cheng Yuanqi
- 邮箱: vixues@gmail.com
- 项目主页: https://github.com/WeiY11/StAgent

---

*最后更�? 2026-08-09*