#!/bin/bash
# Prepara a máquina RunPod para o modo HTTP, num comando só (cole no Web Terminal):
#   curl -sL https://raw.githubusercontent.com/ianmarcos12/runpod-trabalhador/main/instalar_maquina.sh | bash
# Baixa o trabalhador e o vigia, instala o Blender (se faltar), desliga o Jupyter (libera a porta 8888),
# gera o token e liga o trabalhador e o vigia. Pode rodar de novo sem problema (reaproveita o token).
set -e
URL="https://raw.githubusercontent.com/ianmarcos12/runpod-trabalhador/main"
mkdir -p /workspace/maq/runpod /workspace/maq/modelos3d/hdri /workspace/jobs
cd /workspace/maq/runpod
curl -sfL "$URL/trabalhador_http.py" -o trabalhador_http.py
curl -sfL "$URL/parar_se_ocioso.sh" -o parar_se_ocioso.sh
echo "1/4 arquivos baixados: $(ls | tr '\n' ' ')"
if [ ! -x /workspace/blender-linux/blender ]; then
  echo "2/4 instalando o Blender (2 a 5 minutos)..."
  apt-get update -qq >/dev/null 2>&1 || true
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libxi6 libxkbcommon0 libgl1 libxxf86vm1 libxfixes3 libxrender1 libsm6 libegl1 xz-utils wget >/dev/null 2>&1
  cd /workspace
  wget -q -O blender.tar.xz https://download.blender.org/release/Blender5.2/blender-5.2.2-linux-x64.tar.xz
  tar -xf blender.tar.xz && mv blender-5.2.2-linux-x64 blender-linux && rm blender.tar.xz
fi
echo "2/4 $(/workspace/blender-linux/blender --version 2>/dev/null | head -1) | GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null)"
pkill -f jupyter || true
pkill -f trabalhador_http.py || true
sleep 2
[ -s /workspace/.token_trabalhador ] || python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /workspace/.token_trabalhador
chmod 600 /workspace/.token_trabalhador
WORKER_TOKEN="$(cat /workspace/.token_trabalhador)" nohup python3 /workspace/maq/runpod/trabalhador_http.py > /workspace/trabalhador.log 2>&1 &
sleep 2
echo "3/4 $(tail -1 /workspace/trabalhador.log)"
pgrep -f parar_se_ocioso.sh >/dev/null || nohup bash /workspace/maq/runpod/parar_se_ocioso.sh > /workspace/vigia.log 2>&1 &
sleep 1
echo "4/4 vigia: $(tail -1 /workspace/vigia.log 2>/dev/null)"
echo ""
echo "TOKEN (copie para RUNPOD_TOKEN no ambiente do Claude):"
cat /workspace/.token_trabalhador
