"""python -m llmbot  (also: python -m llmbot --mcp-web, the web-tools server Claude Code starts)"""
from llmbot.core import main

if __name__ == "__main__":  # not when a child process (the Screen tab's capture worker) imports it
    main()
