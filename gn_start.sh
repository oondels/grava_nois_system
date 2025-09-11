#!/bin/sh

# O comando 'exec' substitui o processo do shell pelo processo do python.
# Isso garante que o Python receba os sinais do sistema (como o Ctrl+C) corretamente.
# exec python main.py
exec python main.py