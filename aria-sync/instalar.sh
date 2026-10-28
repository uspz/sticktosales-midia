#!/bin/sh
# Liga o Hermes deste PC ao cérebro (Supabase). Pede a chave de serviço sem mostrar na tela.
set -e
D="$HOME/.hermes"; mkdir -p "$D" "$HOME/.local/bin"
curl -fsSL https://raw.githubusercontent.com/uspz/sticktosales-midia/main/aria-sync/sync.py -o "$D/sync.py"
printf 'Cole a chave service_role do Supabase (nao aparece na tela) e de Enter: '
stty -echo </dev/tty; read K </dev/tty; stty echo </dev/tty; echo
[ -n "$K" ] || { echo "Chave vazia."; exit 1; }
umask 077
printf 'SUPABASE_URL=https://jitwxglkpidajurykrmu.supabase.co\nSUPABASE_SERVICE_KEY=%s\n' "$K" > "$D/aria.env"
cat > "$HOME/.local/bin/aria-sync" <<'W'
#!/bin/sh
# envia o que mudou no PC e depois baixa o cerebro (skills e memorias)
set -a; . "$HOME/.hermes/aria.env"; set +a
export ARIA_SO_MEMORIA=1
cd "$HOME/.hermes"
python3 sync.py push && python3 sync.py pull
W
chmod +x "$HOME/.local/bin/aria-sync"
PATH="$HOME/.local/bin:$PATH" aria-sync
echo "Pronto. Para sincronizar de novo: aria-sync"
