# Geoguessr Challenge Bot

The Geoguessr Challenge Bot allows your friends to play single-player Geoguessr for free through challenge links. However, you will need a Geoguessr Pro account in order for the bot to create challenges.

How this works:
1. The bot logs into your Geoguessr account through a headless browser.
2. Anyone can use the `/geochallenge` command to create a challenge link.
3. The bot will create a challenge link and send it to the user.

Additionally, you can set the bot up to automatically send daily challenge links by using the `/setupgeodaily` command.

Please note that this bot is not affiliated with Geoguessr, and we are not responsible for any consequences that may arise from using this bot. Use at your own risk.

# Installation

Pre-requisites: `docker`. Without Docker works too, but you will need to figure out how to run the bot yourself.

1. Clone this repository.
2. Create a `.env` file in the root directory with the following variables:
    ```dotenv
    DISCORD_BOT_TOKEN=XXX
    GEOGUESSR_EMAIL=your@email.com
    GEOGUESSR_PASSWORD=password
    AUTHORIZED_GUILDS=12345,12345
    ```
   If you want to make the bot public, you can remove the `AUTHORIZED_GUILDS` variable.
3. Build the Docker image:
    ```bash
    docker build -t geoguessr-challenge-bot .
    ```
4. Run the Docker container:
    ```bash
    docker run -d --name geoguessr-challenge-bot --env-file .env --restart on-failure -v data:/geoguessr/data geoguessr-challenge-bot:latest
    ```

# Usage

The bot has the following commands:
- `/geochallenge`: Create a challenge link.
- `/geodaily`: Get the current daily challenge link.
- `/setupgeodaily`: Set up daily challenge links.
- `/cancelgeodaily`: Disable sending daily challenge links.

Limitations: At the moment, the bot cannot control who can use the commands. This means that anyone in the server can use the commands.

# License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
