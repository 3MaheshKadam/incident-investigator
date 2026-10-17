# Running the stack on AWS (one EC2 instance)

Goal: the same containers, the same detector and the same gateway, running on a real cloud
machine, checked by `deploy.verify`. One small instance is enough. Nothing here needs a secret.

## Before you start (5 minutes, once)

1. **Set a budget alert.** AWS console, Billing, Budgets, create a budget (for example $5) with an
   email alert. Do this first.
2. Pick one region and use it for everything (for example `ap-south-1`).
3. Know your public IP (search "what is my ip"). The only inbound rule will be SSH from that IP.

## Create the instance (console)

| Setting | Value |
| --- | --- |
| Name | `incident-stack` |
| Image | Ubuntu Server 24.04 LTS |
| Type | `t3.small` (2 GB memory; the stack uses about 1 GB) |
| Key pair | create a new one, download the `.pem`, keep it out of git |
| Network, security group | **inbound: SSH (22) from My IP only.** No other inbound rules |
| Storage | 20 GB gp3 |
| Advanced, metadata version | **IMDSv2 only (required)** |
| Advanced, user data | paste the contents of `deploy/aws/user-data.sh` |

All service ports are bound to `127.0.0.1` inside the instance, so nothing is reachable from the
internet even by mistake. You look at the gateway through an SSH tunnel.

## Deploy and verify (from your laptop, WSL or Linux or Git Bash)

```bash
chmod 400 incident-stack.pem
deploy/aws/deploy.sh <instance-public-ip> incident-stack.pem
```

It waits for setup to finish, copies the committed files, starts the containers, and runs
`python3 -m deploy.verify --wait 120 --detect` on the instance. Expect every line to say PASS and the last line to say all checks passed
(for example `12/12 checks passed`). Copy the full output; it is evidence for the write-up.

Look at it yourself:

```bash
ssh -i incident-stack.pem -L 8080:localhost:8080 ubuntu@<ip>
# then in a browser on your laptop: http://localhost:8080/health
# and on the instance:  cd ~/stack && python3 -m deploy.watch --seconds 120
```

## Turn it off

**Stop the instance when you are not using it** (console, Instance state, Stop). A stopped
instance costs only its disk. **Terminate it** and delete the key pair when you are completely
done, and before the credit expires on Nov 5.

## If something fails

| Symptom | Likely cause |
| --- | --- |
| `Permission denied (publickey)` | wrong key file, or you did not `chmod 400` it |
| SSH times out | security group does not allow your current IP (it changes) |
| `docker: command not found` | setup was not finished; wait a minute and run `deploy.sh` again |
| a check fails | the output names the failing step; paste it back and we fix it |
