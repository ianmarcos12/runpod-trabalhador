#!/bin/bash
# Vigia da máquina RunPod: PARA a máquina quando fica ociosa (sem Blender/ffmpeg rodando e sem atividade recente),
# para o dinheiro não vazar se alguém esquecer de desligar. Roda DENTRO da máquina, em segundo plano.
# Uso: nohup bash /workspace/maq/runpod/parar_se_ocioso.sh >/workspace/vigia.log 2>&1 &
# Variáveis: LIMITE_MIN (padrão 20) = minutos ociosa até parar.   Toda tarefa de render faz: touch /workspace/ultima_atividade
LIMITE_MIN="${LIMITE_MIN:-20}"
ATIV=/workspace/ultima_atividade
touch "$ATIV"
# as credenciais do próprio pod ficam no ambiente do processo 1 (não saem da máquina)
POD_ID=$(tr '\0' '\n' < /proc/1/environ | sed -n 's/^RUNPOD_POD_ID=//p')
API=$(tr '\0' '\n' < /proc/1/environ | sed -n 's/^RUNPOD_API_KEY=//p')
[ -n "$POD_ID" ] && [ -n "$API" ] || { echo "sem RUNPOD_POD_ID/API_KEY: vigia não pode parar a máquina"; exit 1; }
runpodctl config --apiKey "$API" >/dev/null 2>&1
echo "vigia ligado: para a máquina $POD_ID após ${LIMITE_MIN} min ociosa ($(date))"
while true; do
  sleep 60
  if pgrep -x blender >/dev/null || pgrep -x ffmpeg >/dev/null || pgrep -f "remotion" >/dev/null; then touch "$ATIV"; continue; fi
  # sessões ssh interativas contam como atividade (ex.: alguém trabalhando na máquina)
  if [ "$(who 2>/dev/null | wc -l)" -gt 0 ]; then touch "$ATIV"; continue; fi
  idle=$(( ( $(date +%s) - $(stat -c %Y "$ATIV") ) / 60 ))
  if [ "$idle" -ge "$LIMITE_MIN" ]; then
    echo "$(date): ociosa há ${idle} min -> parando a máquina $POD_ID"
    runpodctl stop pod "$POD_ID"
    sleep 120
  fi
done
