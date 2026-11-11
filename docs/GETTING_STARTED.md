# Getting started

There are two ways to run the Refund Desk: the **simulation** (a synthetic PayPal, no keys, no network) and **your own PayPal sandbox** (real sandbox calls, test money only). Start with the simulation. Set up the sandbox when you need real PayPal calls, then follow [SANDBOX_TESTING.md](SANDBOX_TESTING.md).

Run every command from the repository root.

## 1. What you need

- **Python 3.11 or newer**, standard library only. On macOS, `python3` may be Apple's 3.9: check with `python3 --version`. With Homebrew's Python, run `eval "$(/opt/homebrew/bin/brew shellenv)"` (and add that line to `~/.zprofile`).
- **pytest**, for the tests only: `python3 -m pip install pytest`. Without installing anything you can use `uvx --python 3.11 --with pytest pytest -q`.

## 2. Run the simulation (no keys)

```zsh
python3 -m app --fresh
```

Open http://127.0.0.1:8765/desk.html and sign in with the passcode printed in the terminal (`demo-passcode` by default). The banner says **SIMULATION**. `--fresh` deletes the simulation's local data (`data/`) first. Stop with Ctrl+C.

## 3. Run the tests

```zsh
python3 -m pytest -q
```

The tests are offline. The live sandbox tests are skipped unless you opt in (see [SANDBOX_TESTING.md](SANDBOX_TESTING.md)).

## 4. Set up your PayPal sandbox (how to get each value)

Sandbox only. Never use Live credentials, a real card, or your real PayPal login inside the sandbox.

### 4.1 A PayPal developer login

Sign in at https://developer.paypal.com with an ordinary PayPal account (a free personal account works; create one if you need to). This login only opens the Developer Dashboard; the app never uses it. PayPal notes that a developer account is enough to test US integrations, and that a Business account is required to go live or to test outside the US.

### 4.2 Client ID and Secret (`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`)

1. In the Developer Dashboard open **Apps & Credentials**. Keep the Sandbox/Live switch on **Sandbox**.
2. Under **REST API apps**, open **Default Application**, or click **Create App** for a separate one.
3. Copy the **Client ID**. Reveal and copy the **Secret**.

### 4.3 The sandbox test accounts (buyer and shop)

Open **Testing Tools > Sandbox Accounts**. PayPal created two accounts for you:

- **Personal**: the buyer. You log in as this account to approve test payments.
- **Business**: the shop. Refunds come from it, and its activity list shows them.

Click **⋮ > View/Edit Account** to see an account's email (it looks like `sb-...@personal.example.com`) and password.

If a sandbox login says "Some of your information isn't correct", you are using the wrong account (your real login, or the Business account where the buyer is needed) or the wrong site (the address must start with `www.sandbox.paypal.com`). Setting a new password on the View/Edit Account page and typing it usually fixes it.

### 4.4 Your `.env.local`

```zsh
cp -n .env.example .env.local
chmod 600 .env.local
```

Open `.env.local` in an editor and fill in:

- `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` from 4.2.
- `APPROVAL_RECEIPT_HMAC_KEY`: a long random string you keep. Make one with `python3 -c "import secrets; print(secrets.token_hex(32))"`. Records signed with it can only be reopened with the same key.
- `APP_PASSCODE`: any passcode for the merchant pages in sandbox mode.

`.env.local` is git-ignored. Never commit it, and never paste its values into chat, issues or pull requests. Load it into every new terminal:

```zsh
set -a; source .env.local; set +a
```

Then check the setup. The first command makes no network call; the second asks PayPal for one token (no money moves):

```zsh
python3 scripts/sandbox_check.py
python3 scripts/sandbox_check.py --token --yes
```

Expect `RESULT: READY`. A refused token means a wrong ID or Secret, keys from a Live app, or a pasted space.

### 4.5 A captured test payment (the capture ID)

A refund always refers to a payment that already happened, identified by its **capture ID**. In the sandbox you make that payment yourself:

```zsh
python3 scripts/seed_sandbox_capture.py --amount 50.00 --yes
```

1. It prints an approval link. Open it in a private browser window.
2. Log in as the sandbox **Personal** account (4.3).
3. If PayPal asks for a payment method, add a **test card** from PayPal's [credit card generator](https://developer.paypal.com/tools/sandbox/card-testing/): choose Visa and United States, click Generate Credit Card, and use any future expiry date and any 3 digit security code. Never a real card.
4. Click **Continue to Review Order**. That click approves the payment; there is no separate Pay button.
5. Back in the terminal, press **Enter**. The last line is `CAPTURE ID: ...`: that is the capture ID.

The earlier `order created:` line shows the **order ID**, which is not the capture ID (`scripts/sandbox_orders.py` accepts either). If you press Enter before approving, the capture fails; run the command again.

Use a fresh payment for each test run: the app cannot see refunds it did not make itself.

Next: [SANDBOX_TESTING.md](SANDBOX_TESTING.md).
