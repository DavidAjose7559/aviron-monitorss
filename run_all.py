import subprocess, sys

print("[1/2] Running news...")
subprocess.run([sys.executable, "news.py"], check=False)

print("[2/2] Running price watch...")
subprocess.run([sys.executable, "watch.py"], check=False)
