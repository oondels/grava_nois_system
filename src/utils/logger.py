"""Centralized logger configuration for the Grava Nois edge runtime."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logger(
    name: str = "grava_nois",
    *,
    file_name: str = "app.log",
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
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
    logger.propagate = False

    # Formato detalhado para logs
    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-8s] [%(name)s:%(funcName)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Handler para console (INFO)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Handler para arquivo rotativo (DEBUG)
    # Usa fallback relativo à raiz do projeto se GN_LOG_DIR não estiver definido
    base_dir = Path(__file__).resolve().parent.parent.parent
    default_log_dir = base_dir / "logs"
    log_dir = Path(os.getenv("GN_LOG_DIR", default_log_dir))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning(
            f"GN_LOG_DIR inválido ou sem permissão ({log_dir}): {e}. "
            f"Usando fallback local: {default_log_dir}"
        )
        log_dir = default_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / file_name

    # Arquivo rotativo: máximo 10MB por arquivo, mantém 5 backups
    file_handler = RotatingFileHandler(
        filename=log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.info(
        "Logger configurado: console (%s) + arquivo rotativo %s (%s)",
        logging.getLevelName(console_level),
        log_file,
        logging.getLevelName(file_level),
    )

    return logger


# Logger padrão para uso direto
logger = setup_logger()
