# Define the main function to ensure compatibility with module import
__all__ = ["main"]

def main() -> None:
    """Função principal do bot."""
    # Configura logging
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    if not TELEGRAM_BOT_TOKEN:
        logging.error("Variável TELEGRAM_BOT_TOKEN não definida.")
        return
    
    # Cria a Application
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    
    # Registra handlers de comando para comandos específicos
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("scan", cmd_scan))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("debug", cmd_debug))
    application.add_handler(CommandHandler("links", cmd_links))
    application.add_handler(CommandHandler("prelive", cmd_prelive))
    application.add_handler(CommandHandler("prelive_next", cmd_prelive_next))
    application.add_handler(CommandHandler("prelive_show", cmd_prelive_show))
    application.add_handler(CommandHandler("prelive_status", cmd_prelive_status))
    
    # Sinalizadores para graceful shutdown
    stop_event = asyncio.Event()
    
    def signal_handler(signum, frame):
        logging.info("Sinal de shutdown recebido (%s).", signum)
        stop_event.set()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Inicia o bot
    logging.info("EvRadar PRO v0.3-lite MODIFICADO iniciando...")
    
    try:
        # Start bot polling with allowed_updates
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            stop_signals=[],
        )
    except KeyboardInterrupt:
        logging.info("Bot interrompido pelo usuário.")
    finally:
        logging.info("EvRadar PRO encerrado.")

if __name__ == "__main__":
    main()