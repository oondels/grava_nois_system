"""
Módulo de logging centralizado para o sistema Grava Nois.

Configura um logger que escreve:
- Console: nível INFO
- Arquivo rotativo: nível DEBUG (logs/app.log)
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logger(name: str = "grava_nois") -> logging.Logger:
    """
    Configura e retorna um logger com saída para console e arquivo rotativo.

    Args:
        name: Nome do logger (padrão: "grava_nois")

    Returns:
        Logger configurado
    """
    logger = logging.getLogger(name)

    # Evita configuração duplicada
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Formato detalhado para logs
    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-8s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Handler para console (INFO)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Handler para arquivo rotativo (DEBUG)
    # Usa caminho relativo ao diretório atual se GN_LOG_DIR não estiver definido
    default_log_dir = os.getenv("GN_LOG_DIR")
    if default_log_dir:
        log_dir = Path(default_log_dir)
    else:
        # Tenta usar /usr/src/app/logs se existir (container Docker), senão usa ./logs
        docker_log_dir = Path("/usr/src/app/logs")
        if docker_log_dir.parent.exists() and os.access(docker_log_dir.parent, os.W_OK):
            log_dir = docker_log_dir
        else:
            log_dir = Path.cwd() / "logs"

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    # Arquivo rotativo: máximo 10MB por arquivo, mantém 5 backups
    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info(f"Logger configurado: console (INFO) + arquivo rotativo {log_file} (DEBUG)")

    return logger


# Logger padrão para uso direto
logger = setup_logger()
