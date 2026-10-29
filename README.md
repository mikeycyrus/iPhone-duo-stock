# iPhone Duo pickup stock

This watches pickup stock for the iPhone Duo at five Apple Stores in Melbourne and puts the result on a simple phone-friendly page.

The page is:

**https://mikeycyrus.github.io/iphone-duo-stock/**

Open that on your phone. Green means Apple is offering pickup for that model at that store. The page refreshes itself about once a minute. Times are Melbourne time.

It covers these stores:

- Highpoint
- Chadstone
- Doncaster
- Southland
- Fountain Gate

And these models:

- iPhone Duo 256GB Star White
- iPhone Duo 512GB Star White

Each square also shows the last time that model was seen in stock at that store. Under the grid, Recent changes lists when something came into stock or sold out.

You do not need to install anything, pay anything, or leave a computer on. GitHub runs the check for free.

## Phone alerts

You can get one notification on your phone when a model comes into stock at one of these stores. You will not get another one for the same phone while it stays in stock. If it sells out and comes back later, you get a new alert.

1. Install the free **ntfy** app on your iPhone: [ntfy.sh](https://ntfy.sh).
2. In the app, subscribe to a topic. Make the name long and random, like a password. Anyone who knows the name can see the alerts.
3. Tell this project the topic name:
   - Open [this secrets page](https://github.com/mikeycyrus/iphone-duo-stock/settings/secrets/actions).
   - Choose **New repository secret**.
   - Name: `NTFY_TOPIC`
   - Secret: the topic name only, or the full `https://ntfy.sh/your-topic` link.
   - Save.

The next check will start sending alerts. If something is already in stock when you add the secret, you get one alert for it on the following check, as long as it is still available. After that, an alert is sent only when a model comes into stock. You will not get a repeat while it stays in stock. Until the secret exists, the page still works and says phone alerts are not set up.

Tapping an alert opens Apple’s buy page for that model, or the stock page if several arrived at once.

## How often it checks

Each run checks once a minute, about five times, then starts the next run by itself. GitHub’s every-5-minutes timer is only a backup, in case that chain stops. A phone alert is sent as soon as something comes into stock, without waiting for the run to finish.

If a run is still going when the next one is ready, the new one waits. They never check Apple at the same time.

To run one immediately:

1. Open the [Actions tab](https://github.com/mikeycyrus/iphone-duo-stock/actions/workflows/check.yml).
2. Choose **Check pickup stock**.
3. Choose **Run workflow**, then **Run workflow** again.

## If the page says Apple blocked the check

Apple sometimes refuses requests that come from GitHub’s computers. The page will say so in plain language, including the error Apple sent (often HTTP 403).

That message means the check could not read stock. It does **not** mean the phones are sold out. The grid stays on the last reading that worked, and the next check tries again.

## What gets saved

The webpage, including the last-checked time and the recent-changes list, is updated after every minute’s check.

The project’s saved history gets one new version at the end of each five-minute run, so it does not fill up with a commit every minute. That version includes the last-checked time. The same “in stock” alert is not sent again while that phone stays in stock.

Every change from in stock to out of stock, and the other way around, is stored with the Melbourne time. The page keeps the latest of those, and the full list is in `state/history.json`.

## If something in GitHub needs a switch turned on

The page is published with GitHub Pages, using the **GitHub Actions** source. If the link above does not open:

1. Open [Pages settings](https://github.com/mikeycyrus/iphone-duo-stock/settings/pages).
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.

If a check says it could not save stock history:

1. Open [Actions settings](https://github.com/mikeycyrus/iphone-duo-stock/settings/actions).
2. Under **Workflow permissions**, choose **Read and write permissions**, then save.

## Notes for later

The checker is `check.py`. It uses only Python’s standard library. It asks Apple’s public pickup page for postcode 3000 and keeps these stores: Highpoint `R504`, Chadstone `R180`, Doncaster `R342`, Southland `R343`, Fountain Gate `R530`.

To watch another model, add its part number to `WATCHED_PARTS` at the top of `check.py`. The full list, which you can copy from, is just below that:

- `MK2D4X/A` 256GB Star White (watched)
- `MK2E4X/A` 256GB Night Sky
- `MK2F4X/A` 512GB Star White (watched)
- `MK2G4X/A` 512GB Night Sky
- `MK2H4X/A` 1TB Star White
- `MK2J4X/A` 1TB Night Sky
- `MK2K4X/A` 2TB Star White
- `MK2L4X/A` 2TB Night Sky

The scheduled task is `.github/workflows/check.yml`. The webpage is the `docs` folder.
