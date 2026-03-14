from investopedia_api import TradeAPI

def main() -> None:
    TradeAPI.login_and_save_session()
    print("finished")


if __name__ == "__main__":
    main()
