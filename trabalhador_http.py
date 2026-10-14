"""Trabalhador HTTP da máquina RunPod: recebe arquivos, roda o Blender e devolve os quadros — tudo por HTTPS pelo proxy da RunPod
(https://<ID_DO_POD>-8888.proxy.runpod.net), para sessões cuja rede bloqueia SSH. Só biblioteca padrão do Python.

Roda NA máquina RunPod:   WORKER_TOKEN='<segredo longo>' nohup python3 /workspace/maq/runpod/trabalhador_http.py > /workspace/trabalhador.log 2>&1 &
Variáveis:  WORKER_TOKEN (obrigatória, 24+ caracteres)   PORTA (padrão 8888)   WORKSPACE (padrão /workspace)
            BLENDER (padrão $WORKSPACE/blender-linux/blender)

Toda rota exige o cabeçalho  Authorization: Bearer <WORKER_TOKEN>  (comparado em tempo constante).
  GET    /saude                    placa de vídeo, Blender e HDRIs disponíveis
  POST   /enviar/<job>/<nome>      corpo = conteúdo do arquivo -> jobs/<job>/in/<nome>  (.hdr vai para a pasta comum de HDRIs)
  POST   /iniciar/<job>            JSON {"script": "giro_produto.py", "args": [...], "env": {...}}; "{in}"/"{out}" viram as pastas do job.
                                   O Blender roda separado do pedido (a resposta volta na hora) com o log em jobs/<job>/render.log
  GET    /estado/<job>             {"quadros": n, "vivo": bool, "codigo": int|null, "log": "fim do log"}
  GET    /zip/<job>                quadros f_* (e cena.blend) num zip sem compressão
  DELETE /limpar/<job>             para o Blender do job (se estiver rodando) e apaga a pasta

Segurança: quem tem o token roda Python dentro do Blender nesta máquina (igual a ter o SSH). Use um token longo e aleatório,
guarde-o só nas variáveis de ambiente (RUNPOD_TOKEN na sessão) e troque se vazar.
"""
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("WORKER_TOKEN", "")
PORTA = int(os.environ.get("PORTA") or 8888)
WORKSPACE = os.environ.get("WORKSPACE") or "/workspace"
BLENDER = os.environ.get("BLENDER") or os.path.join(WORKSPACE, "blender-linux", "blender")
JOBS = os.path.join(WORKSPACE, "jobs")
MODELOS3D = os.path.join(WORKSPACE, "maq", "modelos3d")
HDRI = os.path.join(MODELOS3D, "hdri")
ATIVIDADE = os.path.join(WORKSPACE, "ultima_atividade")  # o vigia (parar_se_ocioso.sh) só para a máquina se isso ficar velho
MAX_ENVIO = 300 * 1024 * 1024
RE_JOB = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RE_NOME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}$")
RE_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
ENV_PROIBIDAS = {"PATH", "HOME", "PYTHONPATH", "PYTHONHOME", "WORKER_TOKEN"}
PROCESSOS = {}  # job -> Popen
TRAVA = threading.Lock()


def pastas(job):
    base = os.path.join(JOBS, job)
    return base, os.path.join(base, "in"), os.path.join(base, "out")


def tocar_atividade():
    try:
        with open(ATIVIDADE, "a"):
            os.utime(ATIVIDADE, None)
    except OSError:
        pass


def vivo(job):
    p = PROCESSOS.get(job)
    if p is not None:
        return p.poll() is None
    try:  # o trabalhador pode ter reiniciado: confere pelo pid gravado
        pid = int(open(os.path.join(JOBS, job, "pid")).read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def quadros_prontos(saida):
    if not os.path.isdir(saida):
        return []
    return sorted(f for f in os.listdir(saida) if re.match(r"^f_\d{4,}\.(jpg|png)$", f))


def saude():
    gpu = None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=20)
        gpu = r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        pass
    versao = None
    if os.access(BLENDER, os.X_OK):
        try:
            versao = subprocess.run([BLENDER, "--version"], capture_output=True, text=True, timeout=60).stdout.splitlines()[0].strip()
        except (OSError, subprocess.SubprocessError, IndexError):
            versao = "?"
    hdris = sorted(f for f in os.listdir(HDRI) if f.endswith(".hdr")) if os.path.isdir(HDRI) else []
    rodando = [j for j in list(PROCESSOS) if vivo(j)]
    return {"ok": bool(versao), "gpu": gpu, "blender": versao, "hdri": hdris, "rodando": rodando}


def iniciar(job, pedido):
    base, ent, sai = pastas(job)
    script = str(pedido.get("script") or "")
    if not RE_NOME.match(script) or not script.endswith(".py") or not os.path.isfile(os.path.join(ent, script)):
        return 400, {"erro": "script inválido ou não enviado (mande antes por /enviar/<job>/<script>)"}
    args = pedido.get("args") or []
    env_extra = pedido.get("env") or {}
    if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args) or len(args) > 40:
        return 400, {"erro": "args deve ser uma lista de textos"}
    if not isinstance(env_extra, dict):
        return 400, {"erro": "env deve ser um objeto"}
    troca = lambda v: str(v).replace("{in}", ent).replace("{out}", sai)  # noqa: E731
    env = dict(os.environ)
    env.pop("WORKER_TOKEN", None)  # o Blender não precisa do segredo
    for k, v in env_extra.items():
        if not RE_ENV.match(str(k)) or k in ENV_PROIBIDAS or k.startswith(("LD_", "PYTHON")) or v is None:
            return 400, {"erro": f"variável não permitida: {k}"}
        env[k] = troca(v)
    with TRAVA:
        if vivo(job):
            return 409, {"erro": "já existe um render rodando neste job"}
        os.makedirs(sai, exist_ok=True)
        # estudio.py procura os HDRIs em <pasta do script>/../modelos3d/hdri: aponta para a pasta comum da máquina
        ligacao = os.path.join(base, "modelos3d")
        if not os.path.exists(ligacao):
            os.makedirs(HDRI, exist_ok=True)
            os.symlink(MODELOS3D, ligacao)
        log = open(os.path.join(base, "render.log"), "wb")
        cmd = [BLENDER, "-b", "--factory-startup", "-P", os.path.join(ent, script), "--"] + [troca(a) for a in args]
        # nova sessão + entrada/saída desligadas do pedido: o Blender continua rodando depois que a resposta HTTP termina
        p = subprocess.Popen(cmd, cwd=ent, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        PROCESSOS[job] = p
        with open(os.path.join(base, "pid"), "w") as f:
            f.write(str(p.pid))
    threading.Thread(target=p.wait, daemon=True).start()  # recolhe o processo quando acabar (sem zumbi)
    return 200, {"ok": True, "pid": p.pid}


def estado(job):
    base, _, sai = pastas(job)
    if not os.path.isdir(base):
        return 404, {"erro": "job não existe"}
    log = ""
    try:
        with open(os.path.join(base, "render.log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 3000))
            log = f.read().decode("utf-8", "replace")
    except OSError:
        pass
    p = PROCESSOS.get(job)
    return 200, {"quadros": len(quadros_prontos(sai)), "vivo": vivo(job), "codigo": p.poll() if p else None, "log": log[-2000:]}


def limpar(job):
    base, _, _ = pastas(job)
    p = PROCESSOS.pop(job, None)
    pid = p.pid if p else None
    if pid is None:
        try:
            pid = int(open(os.path.join(base, "pid")).read().strip())
        except (OSError, ValueError):
            pid = None
    if pid and (p is None or p.poll() is None):
        try:
            os.killpg(pid, signal.SIGTERM)  # o Blender foi iniciado numa sessão própria: o grupo tem o mesmo número do pid
        except OSError:
            pass
    shutil.rmtree(base, ignore_errors=True)
    return 200, {"ok": True}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "trabalhador"

    def log_message(self, fmt, *a):  # sem o cabeçalho de autorização no log
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

    def _json(self, codigo, dados):
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def _autorizado(self):
        dado = self.headers.get("Authorization", "")
        esperado = "Bearer " + TOKEN
        return hmac.compare_digest(dado.encode("utf-8"), esperado.encode("utf-8"))

    def _rota(self):
        if not self._autorizado():
            # lê e descarta o corpo para não deixar lixo na conexão
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= MAX_ENVIO:
                self.rfile.read(n)
            self._json(401, {"erro": "token inválido"})
            return None
        tocar_atividade()
        partes = [p for p in self.path.split("?")[0].split("/") if p]
        if len(partes) >= 2 and not RE_JOB.match(partes[1]):
            self._json(400, {"erro": "nome de job inválido"})
            return None
        return partes

    def do_GET(self):
        partes = self._rota()
        if partes is None:
            return
        if partes == ["saude"]:
            return self._json(200, saude())
        if len(partes) == 2 and partes[0] == "estado":
            return self._json(*estado(partes[1]))
        if len(partes) == 2 and partes[0] == "zip":
            return self._zip(partes[1])
        self._json(404, {"erro": "rota desconhecida"})

    def do_POST(self):
        partes = self._rota()
        if partes is None:
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_ENVIO:
            return self._json(413, {"erro": "arquivo grande demais"})
        if len(partes) == 3 and partes[0] == "enviar":
            nome = partes[2]
            if not RE_NOME.match(nome):
                self.rfile.read(n)
                return self._json(400, {"erro": "nome de arquivo inválido"})
            _, ent, _ = pastas(partes[1])
            destino_dir = HDRI if nome.endswith(".hdr") else ent  # HDRI fica na pasta comum (reaproveitado entre jobs)
            os.makedirs(destino_dir, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(dir=destino_dir, delete=False, prefix=".envio_")
            try:
                falta = n
                while falta > 0:
                    bloco = self.rfile.read(min(1 << 20, falta))
                    if not bloco:
                        raise OSError("conexão caiu no meio do envio")
                    tmp.write(bloco)
                    falta -= len(bloco)
                tmp.close()
                os.replace(tmp.name, os.path.join(destino_dir, nome))
            except OSError as e:
                tmp.close()
                os.unlink(tmp.name)
                return self._json(400, {"erro": str(e)})
            return self._json(200, {"ok": True, "bytes": n})
        if len(partes) == 2 and partes[0] == "iniciar":
            try:
                pedido = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"erro": "JSON inválido"})
            return self._json(*iniciar(partes[1], pedido))
        self.rfile.read(n)
        self._json(404, {"erro": "rota desconhecida"})

    def do_DELETE(self):
        partes = self._rota()
        if partes is None:
            return
        if len(partes) == 2 and partes[0] == "limpar":
            return self._json(*limpar(partes[1]))
        self._json(404, {"erro": "rota desconhecida"})

    def _zip(self, job):
        _, _, sai = pastas(job)
        nomes = quadros_prontos(sai) + (["cena.blend"] if os.path.isfile(os.path.join(sai, "cena.blend")) else [])
        if not nomes:
            return self._json(404, {"erro": "sem quadros"})
        with tempfile.TemporaryFile() as tmp:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:  # JPEG/PNG já são comprimidos
                for nome in nomes:
                    z.write(os.path.join(sai, nome), nome)
            tamanho = tmp.tell()
            tmp.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(tamanho))
            self.end_headers()
            shutil.copyfileobj(tmp, self.wfile, 1 << 20)


def main():
    if len(TOKEN) < 24:
        sys.exit("defina WORKER_TOKEN com pelo menos 24 caracteres (ex.: python3 -c 'import secrets; print(secrets.token_urlsafe(32))')")
    os.makedirs(JOBS, exist_ok=True)
    os.makedirs(HDRI, exist_ok=True)
    tocar_atividade()
    srv = ThreadingHTTPServer(("0.0.0.0", PORTA), H)
    srv.daemon_threads = True
    print(f"trabalhador ouvindo na porta {PORTA} (Blender: {BLENDER})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
