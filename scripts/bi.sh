#!/usr/bin/env bash
# 本地开发助手（Git Bash / 终端）
#
#   ./scripts/bi.sh run  [dev|test|uat|pro] [port]   启动后端服务（默认 dev / 8100，热重载）
#   ./scripts/bi.sh test                            运行后端 pytest（16 项）
#   ./scripts/bi.sh seed [rows]                     重新生成两套合成数据（默认 10 万行）
#
# 说明：路径由 backend/app/core/config.py 基于文件位置推导，因此在任何目录下执行均可。
set -euo pipefail

PY="C:/Users/William Y Liang/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CMD="${1:-run}"

case "$CMD" in
  run)
    ENV="${2:-dev}"
    PORT="${3:-8100}"
    cd "$ROOT/backend"
    echo "启动后端  env=$ENV  http://127.0.0.1:$PORT/docs"
    APP_ENV="$ENV" PYTHONPATH="$ROOT/backend" "$PY" -m uvicorn app.main:app \
      --reload --host 127.0.0.1 --port "$PORT"
    ;;
  test)
    cd "$ROOT/backend"
    APP_ENV=test PYTHONPATH="$ROOT/backend" "$PY" -m pytest -v
    ;;
  seed)
    ROWS="${2:-100000}"
    "$PY" "$ROOT/scripts/gen_synthetic_data.py" --dataset both --rows "$ROWS"
    ;;
  *)
    echo "用法: ./scripts/bi.sh {run|test|seed} [参数]"
    echo "  run  [dev|test|uat|pro] [port]   启动服务（默认 dev / 8100）"
    echo "  test                            运行 pytest"
    echo "  seed [rows]                     生成合成数据（默认 100000）"
    exit 1
    ;;
esac
