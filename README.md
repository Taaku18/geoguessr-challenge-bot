# Geoguessr Challenge Bot

The Geoguessr Challenge Bot allows your friends to play single-player Geoguessr for free through challenge links. However, you will need a Geoguessr Pro account in order for the bot to create challenges.

How this works:
1. Anyone can use the `/geochallenge` command to create a challenge link.
2. The bot will create a challenge link and send it to the user.

Additionally, you can set the bot up to automatically send daily challenge links by using the `/setupgeodaily` command.

Please note that this bot is not affiliated with Geoguessr, and we are not responsible for any consequences that may arise from using this bot (e.g. account ban). Use at your own risk.

# Installation

Pre-requisites: `docker`. Without Docker works too, but you will need to figure out how to run the bot yourself.

1. Clone this repository.
2. Create a `.env` file in the root directory with the following variables:
    ```dotenv
    DISCORD_BOT_TOKEN=XXX
    GEOGUESSR_AUTO_USERNAME=auto-username
    AUTHORIZED_GUILDS=12345,12345
    ```
   If you want to make the bot public, you can remove the `AUTHORIZED_GUILDS` variable.
   The `GEOGUESSR_AUTO_USERNAME` is used to auto guess daily challenges (necessary to display the leaderboard). Since this bot will always guess in Antarctica, this will tank your Geoguessr average score, so the solution is to make a dedicated (free) account for auto-guessing. If you do not wish to use a dedicated auto-guessing account, you can set `GEOGUESSR_AUTO_USERNAME` to your main account's username and set the session token to your main account's.

3. Build the Docker image:
    ```bash
    docker build -t geoguessr-challenge-bot .
    ```
4. Run the Docker container:
    ```bash
    docker run -d --name geoguessr-challenge-bot --env-file .env --restart on-failure -v data:/geoguessr/data geoguessr-challenge-bot:latest
    ```

# Usage

Set the session token for your Geoguessr account by DMing the bot (as the bot owner) with the following command:
```
!maintoken MAIN_SESSION_TOKEN
!autotoken AUTO_SESSION_TOKEN
```
The session tokens can be found by:

1. Login to Geoguessr in your web browser. 
2. Open the developer tools and navigate Application → Storage → Cookies. 
3. Copy the value of the `_ncfa` cookie (that's the account's session cookie).

Unfortunately, the bot cannot retrieve the session token for you due to Cloudflare bot protection, so you will need to do this manually. The session token may expire after a while, so you may need to update occasionally.

The bot has the following commands:
- `/geochallenge`: Create a challenge link.
- `/geodaily`: Get the current daily challenge link.
- `/setupgeodaily`: Set up daily challenge links.
- `/cancelgeodaily`: Disable sending daily challenge links.

Limitations: At the moment, the bot cannot control who can use the commands. This means that anyone in the server can use the commands.

# License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
