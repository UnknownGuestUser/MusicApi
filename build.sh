#!/usr/bin/env bash
set -e

export DENO_INSTALL="/app/.deno"
export PATH="$DENO_INSTALL/bin:$PATH"

curl -fsSL https://deno.land/install.sh | sh

echo "===== DENO VERSION ====="
"$DENO_INSTALL/bin/deno" --version
echo "========================"
