#!/bin/zsh
set -e
cd -- "${0:A:h}"
demo_env="${XDG_CACHE_HOME:-$HOME/Library/Caches}/campusloop-streamlit/venv"
if [[ ! -x "$demo_env/bin/python" ]]; then
  python3 -m venv "$demo_env"
fi
if ! "$demo_env/bin/python" -c 'import streamlit' >/dev/null 2>&1; then
  "$demo_env/bin/python" -m pip install -r requirements.txt
fi
exec "$demo_env/bin/python" -m streamlit run campusloop.py --server.address localhost --browser.gatherUsageStats false
