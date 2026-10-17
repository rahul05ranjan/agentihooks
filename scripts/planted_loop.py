import time


def beat(work):
    while True:
        work()
        time.sleep(60)
