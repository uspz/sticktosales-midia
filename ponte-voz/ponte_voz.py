#!/usr/bin/env python3
"""Ponte de voz do Escritório (local e grátis).

Ouve o microfone, transforma a fala em texto em português no próprio PC
(faster-whisper ou Vosk, sem internet depois do primeiro download) e digita
no chat do painel do Escritório só quando a frase começa com "Aria", com o
nome de um agente ou com um comando (pausa, continua, status...).

Uso:
  ponte-voz                       # padrão: Whisper "base", palavra de ativação ligada
  ponte-voz --modelo small        # mais preciso, mais pesado
  ponte-voz --motor vosk          # o mais leve (modelo de 31 MB), menos preciso
  ponte-voz --sempre              # envia tudo, sem precisar dizer "Aria"
  ponte-voz --seco                # só mostra o que entendeu, não digita
  ponte-voz --teste fala.wav      # transcreve um arquivo WAV e mostra o resultado
"""
import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import time
import unicodedata

TAXA = 16000
BLOCO = 480  # 30 ms

NOMES = ["aria", "recrutador", "recrutadora", "instrutor", "analista", "enxuto", "redator", "redatora",
         "roteirista", "designer", "programador", "programadora", "desenvolvedor web", "revisor", "revisora",
         "administrador", "postador", "atendente", "jornalista", "advogado", "advogada", "prospector",
         "vendedor", "vendedora", "secretaria", "editor", "editora", "financeiro", "crescimento", "metricas",
         "planilhas", "automacao", "seguranca", "juridico", "conteudo"]
COMANDO = re.compile(r"^(pausa|pausar|continua|continuar|retoma|status|quanto gastei|quantos tokens|"
                     r"confirma|confirmo|cancela|faz no facil|le pra mim|repete|para de falar|silencio|"
                     r"fala com|chama o|chama a|liga a voz|desliga a voz)\b")
INTERJ = re.compile(r"^(ei|oi|o|ola|e ai|hey|alo|bom dia|boa tarde|boa noite)\s+")
ALUCINA = re.compile(r"(legendas? (pela|por)|amara\.org|inscreva-se|obrigad[oa] por assistir|^obrigad[oa]\.?$|^tchau\.?$)", re.I)


def norm(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9@.\s-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def deve_enviar(texto, sempre=False, aberto=False):
    """True se a frase é para o Escritório (palavra de ativação, nome, comando ou conversa aberta)."""
    t = str(texto or "").strip()
    if len(t) < 2 or ALUCINA.search(t):
        return False
    if sempre or aberto:
        return True
    n = INTERJ.sub("", norm(t))
    if any(n == x or n.startswith(x + " ") for x in NOMES):
        return True
    return bool(COMANDO.match(n))


def tem(cmd):
    return shutil.which(cmd) is not None


def avisar(msg):
    print(msg, flush=True)
    if tem("notify-send"):
        subprocess.run(["notify-send", "-t", "2500", "Ponte de voz", msg], check=False)


def digitar(texto, janela):
    """Ativa a janela do painel, cola o texto no chat e aperta Enter."""
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        avisar("Sessão Wayland: o xdotool não consegue digitar. Entre na sessão X11 (Cinnamon padrão do Mint).")
        return False
    r = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", janela], capture_output=True, text=True)
    ids = [x for x in r.stdout.split() if x.strip()]
    if not ids:
        avisar(f'Não achei a janela do painel ("{janela}"). Abra o Escritório no navegador e clique no chat.')
        return False
    antigo = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True).stdout
    subprocess.run(["xdotool", "windowactivate", "--sync", ids[-1]], check=False)
    time.sleep(0.15)
    subprocess.run(["xclip", "-selection", "clipboard"], input=texto.encode("utf-8"), check=False)
    subprocess.run(["xdotool", "key", "--clearmodifiers", "ctrl+v"], check=False)
    time.sleep(0.08)
    subprocess.run(["xdotool", "key", "--clearmodifiers", "Return"], check=False)
    time.sleep(0.3)
    subprocess.run(["xclip", "-selection", "clipboard"], input=antigo, check=False)
    return True


def motor_whisper(modelo):
    from faster_whisper import WhisperModel
    print(f"Carregando o Whisper '{modelo}' (na primeira vez ele baixa o modelo)…", flush=True)
    m = WhisperModel(modelo, device="cpu", compute_type="int8")

    def transcrever(audio):
        segs, _ = m.transcribe(audio, language="pt", beam_size=1, vad_filter=True,
                               condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segs).strip()
    return transcrever


def motor_vosk(caminho):
    import json
    from vosk import KaldiRecognizer, Model, SetLogLevel
    SetLogLevel(-1)
    if not os.path.isdir(caminho):
        sys.exit(f"Modelo Vosk não encontrado em {caminho}. Rode o instalador com --vosk.")
    m = Model(caminho)

    def transcrever(audio):
        import numpy as np
        rec = KaldiRecognizer(m, TAXA)
        rec.AcceptWaveform((np.clip(audio, -1, 1) * 32767).astype("int16").tobytes())
        return json.loads(rec.FinalResult()).get("text", "").strip()
    return transcrever


def ler_wav(caminho):
    import wave
    import numpy as np
    with wave.open(caminho, "rb") as w:
        if w.getframerate() != TAXA or w.getnchannels() != 1 or w.getsampwidth() != 2:
            sys.exit("Use WAV mono 16 kHz 16 bits (ffmpeg -i entrada -ac 1 -ar 16000 fala.wav).")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="int16").astype("float32") / 32768.0


class Cortador:
    """Separa as falas pelo volume: começa quando passa do limiar e fecha após 0,8 s de silêncio."""

    def __init__(self, limiar_min=0.012, silencio_s=0.8, max_s=15.0, min_s=0.45):
        self.limiar_min, self.silencio, self.max, self.min = limiar_min, silencio_s, max_s, min_s
        self.ruido, self.buf, self.falando, self.quietos, self.altos = 0.005, [], False, 0, 0

    def passo(self, bloco):
        import numpy as np
        rms = float(np.sqrt(np.mean(bloco ** 2)) + 1e-9)
        lim = max(self.limiar_min, self.ruido * 3.0)
        if not self.falando:
            self.ruido = 0.97 * self.ruido + 0.03 * min(rms, 0.05)
            self.buf = (self.buf + [bloco])[-10:]  # 300 ms antes da fala
            self.altos = self.altos + 1 if rms > lim else 0
            if self.altos >= 3:
                self.falando, self.quietos = True, 0
            return None
        self.buf.append(bloco)
        self.quietos = self.quietos + 1 if rms < lim * 0.7 else 0
        dur = len(self.buf) * len(bloco) / TAXA
        if self.quietos * len(bloco) / TAXA >= self.silencio or dur >= self.max:
            audio = np.concatenate(self.buf)
            self.buf, self.falando, self.altos = [], False, 0
            return audio if dur >= self.min else None
        return None


def main():
    ap = argparse.ArgumentParser(description="Ponte de voz do Escritório (local e grátis)")
    ap.add_argument("--motor", choices=["whisper", "vosk"], default="whisper")
    ap.add_argument("--modelo", default="base", help="Whisper: tiny, base (padrão), small")
    ap.add_argument("--vosk-modelo", default=os.path.expanduser("~/.local/share/escritorio-voz/vosk-model-small-pt-0.3"))
    ap.add_argument("--janela", default="Escritório", help="parte do título da janela do painel")
    ap.add_argument("--sempre", action="store_true", help="envia tudo, sem palavra de ativação")
    ap.add_argument("--seco", action="store_true", help="só mostra, não digita")
    ap.add_argument("--teste", metavar="WAV", help="transcreve um arquivo WAV e sai")
    a = ap.parse_args()

    transcrever = motor_whisper(a.modelo) if a.motor == "whisper" else motor_vosk(a.vosk_modelo)
    if a.teste:
        txt = transcrever(ler_wav(a.teste))
        print(f"Entendi: {txt}\nEnviaria para o painel: {'sim' if deve_enviar(txt, a.sempre) else 'não (falta dizer Aria)'}")
        return
    if not a.seco and not (tem("xdotool") and tem("xclip")):
        sys.exit("Falta o xdotool ou o xclip: sudo apt install xdotool xclip")

    import sounddevice as sd
    fila = queue.Queue()
    corte = Cortador()
    aberto_ate = 0.0

    def cb(indata, frames, t, status):
        fila.put(indata[:, 0].copy())

    avisar('Ponte de voz ligada. Clique uma vez no chat do painel e fale: "Aria, status do time".')
    with sd.InputStream(samplerate=TAXA, channels=1, dtype="float32", blocksize=BLOCO, callback=cb):
        while True:
            audio = corte.passo(fila.get())
            if audio is None:
                continue
            txt = transcrever(audio)
            if not txt:
                continue
            aberto = time.time() < aberto_ate
            if deve_enviar(txt, a.sempre, aberto):
                print(f"> {txt}", flush=True)
                if a.seco or digitar(txt, a.janela):
                    aberto_ate = time.time() + 20
            else:
                print(f"  (ignorado, diga “Aria” antes) {txt}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nPonte de voz desligada.")
