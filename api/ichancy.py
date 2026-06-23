import cloudscraper

class IchancyAPI:
    def __init__(self, username, password):
        self.scraper = cloudscraper.create_scraper()
        self.base_url = "https://agents.ichancy.com/global/api"
        self.auth = (username, password)
        self._login()

    def _login(self):
        self.scraper.post(
            f"{self.base_url}/User/signIn",
            json={"username": self.auth[0], "password": self.auth[1]}
        )

    def register_player(self, email, password, login, country="SY"):
        return self.scraper.post(
            f"{self.base_url}/Player/registerPlayer",
            json={"player": {
                "email": email,
                "password": password,
                "parentId": "2751155",
                "login": login,
                "countryCode": country
            }},
            auth=self.auth
        ).json()

    def deposit(self, player_id, amount, currency="NSP"):
        return self.scraper.post(
            f"{self.base_url}/Player/depositToPlayer",
            json={
                "amount": amount,
                "playerId": player_id,
                "currencyCode": currency,
                "moneyStatus": 5,
                "comment": None
            },
            auth=self.auth
        ).json()

    def withdraw(self, player_id, amount, currency="NSP"):
        return self.scraper.post(
            f"{self.base_url}/Player/withdrawFromPlayer",
            json={
                "amount": -abs(amount),
                "playerId": player_id,
                "currencyCode": currency,
                "moneyStatus": 5,
                "comment": None
            },
            auth=self.auth
        ).json()

    def get_balance(self, player_id):
        return self.scraper.post(
            f"{self.base_url}/Player/getPlayerBalanceById",
            json={"playerId": player_id},
            auth=self.auth
        ).json()
