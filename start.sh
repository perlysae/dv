#!/bin/bash
# Jalankan di terminal Codespaces: bash start.sh
set -e
sudo apt-get update -y
sudo apt-get install -y ffmpeg gpac
curl -L https://github.com/quietvoid/dovi_tool/releases/download/2.1.2/dovi_tool-2.1.2-x86_64-unknown-linux-musl.tar.gz | sudo tar xz -C /usr/local/bin dovi_tool
pip install -r requirements.txt
python app.py
