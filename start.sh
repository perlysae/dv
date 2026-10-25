#!/bin/bash
# Jalankan di terminal Codespaces: bash start.sh
cd "$(dirname "$0")"

echo "== 1/4 ffmpeg =="
sudo apt-get update -y
sudo apt-get install -y ffmpeg

echo "== 2/4 dovi_tool =="
if ! command -v dovi_tool >/dev/null; then
  curl -L https://github.com/quietvoid/dovi_tool/releases/download/2.1.2/dovi_tool-2.1.2-x86_64-unknown-linux-musl.tar.gz \
    | sudo tar xz -C /usr/local/bin dovi_tool
fi

echo "== 3/4 MP4Box (gpac) =="
if ! command -v MP4Box >/dev/null; then
  if ! sudo apt-get install -y gpac; then
    curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj bin/micromamba
    MAMBA_ROOT_PREFIX=$HOME/mm ./bin/micromamba create -y -n gp -c conda-forge gpac
    printf '#!/bin/bash\nexport LD_LIBRARY_PATH=$HOME/mm/envs/gp/lib:$LD_LIBRARY_PATH\nexec $HOME/mm/envs/gp/bin/MP4Box "$@"\n' | sudo tee /usr/local/bin/MP4Box >/dev/null
    sudo chmod +x /usr/local/bin/MP4Box
  fi
fi

echo "== 4/4 python =="
pip install -r requirements.txt

echo ""
echo "CEK TOOL:"
ffmpeg -version | head -1
dovi_tool --version
MP4Box -version 2>&1 | head -1
echo ""
echo "SERVER JALAN. Buka tab PORTS, tekan ikon globe di baris 8000."
python app.py
