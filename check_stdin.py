import sys
import os
import time

print("--- Iniciando diagnóstico de STDIN ---")

# isatty() retorna True se o stream estiver conectado a um dispositivo TTY (interativo)
is_a_tty = sys.stdin.isatty()

print(f"sys.stdin.isatty() retornou: {is_a_tty}")
print(f"Variável de ambiente TERM: {os.getenv('TERM')}")

if is_a_tty:
    print("\n>>> SUCESSO: Python detectou um terminal interativo (TTY).")
    print(">>> O ambiente Docker parece estar correto. O problema pode ser outro.")
    print(">>> Por favor, digite algo e pressione ENTER para confirmar a leitura:")
    try:
        line = sys.stdin.readline()
        print(f"\n>>> DADOS RECEBIDOS COM SUCESSO: '{line.strip()}'")
    except Exception as e:
        print(f"\n>>> ERRO Inesperado ao tentar ler stdin: {e}")
else:
    print("\n>>> FALHA: Python NÃO detectou um terminal interativo (TTY).")
    print(">>> ESTA É A CAUSA DO PROBLEMA. O 'input()' do Python não funciona sem um TTY.")
    print("\n>>> SOLUÇÃO: Rode o comando 'docker-compose up --build' a partir de um TERMINAL PADRÃO do seu sistema operacional, e NÃO de um terminal embutido em uma IDE (como VS Code).")

print("\n--- Diagnóstico concluído. Encerrando em 10 segundos. ---")
time.sleep(10)