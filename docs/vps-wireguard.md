# Reaching a locked-down VPS with a dedicated WireGuard link

Use this when the VPS only accepts SSH from your PC and denies everything else, and the machine
running Homelab Manager (the Docker host, for example the Arcane LXC) is somewhere else, so the
dashboard cannot SSH in. You add a small, separate WireGuard tunnel just for this. SSH then
arrives over the tunnel, so no public SSH port is opened and your PC's rule stays as it is.

It runs next to Pangolin without touching it: Pangolin's Gerbil uses `wg0` on UDP 51820, this uses
its own interface `wg1` on UDP 51821. It also works where Tailscale can't be installed.

```
Docker host (LXC)  10.99.0.2  --- wg1, UDP 51821 --->  10.99.0.1  VPS
  dashboard container -> SSH to 10.99.0.1:22 (source address 10.99.0.2)
```

The Docker host sits behind home NAT, so it always dials out to the VPS. The VPS side needs no
address for it, only the public key.

Placeholders: `<VPS_IP>` is the VPS public address, `<PC_IP>` is the public IP your PC SSHes from,
`<you>` is your own login on the VPS, `<PROJECT>` is the Arcane project folder.

## 0. Before you start

- **Keep a second SSH session to the VPS open the whole time**, and know how to reach your
  provider's web console. Every firewall step below only *adds* an allow rule, so you are unlikely
  to lock yourself out, but the open session and the console are the way back if you do.
- Pick a tunnel network that nothing on either machine uses. `10.99.0.0/24` is used here. On the VPS
  run `ip -br addr; ip route` and make sure nothing there (Pangolin, Docker, the provider's private
  network) already uses `10.99.x.x`. If it does, pick another `/24` and substitute it everywhere.
- Check the port is free on the VPS: `sudo ss -ulnp | grep 51821` should print nothing.

## 1. VPS: install WireGuard and make keys

```bash
sudo apt update && sudo apt install -y wireguard-tools
sudo sh -c 'umask 077; mkdir -p /etc/wireguard; wg genkey | tee /etc/wireguard/wg1.key | wg pubkey > /etc/wireguard/wg1.pub'
sudo cat /etc/wireguard/wg1.pub     # the VPS public key, you need it in step 4
```

Ubuntu 22.04 and newer have WireGuard in the kernel, so `wireguard-tools` is enough. The private key
never leaves the VPS. Only the `.pub` files are ever copied between machines.

## 2. Docker host (LXC): let it use WireGuard

An unprivileged LXC can't load kernel modules, and it needs the TUN device. On the **Proxmox host**:

```bash
modprobe wireguard                      # PVE 7.x/8.x kernels ship it; lsmod | grep wireguard to confirm
echo wireguard >> /etc/modules-load.d/wireguard.conf
```

Then add to `/etc/pve/lxc/<VMID>.conf` (the Arcane LXC):

```
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
```

On newer Proxmox versions you can instead use the device passthrough in the GUI (LXC > Resources >
Add > Device Passthrough, path `/dev/net/tun`). Reboot the LXC (`pct reboot <VMID>`) and check inside
it that `ls -l /dev/net/tun` exists.

If the LXC still can't create the interface (`RTNETLINK answers: Operation not supported`), the host
has no WireGuard module. Install the userspace implementation in the LXC instead:
`apt install -y wireguard-go wireguard-tools`. `wg-quick` picks it up automatically when the kernel
interface fails, with nothing else changed below.

## 3. Docker host (LXC): install and make keys

Run inside the LXC:

```bash
apt update && apt install -y wireguard-tools
sh -c 'umask 077; mkdir -p /etc/wireguard; wg genkey | tee /etc/wireguard/wg1.key | wg pubkey > /etc/wireguard/wg1.pub'
cat /etc/wireguard/wg1.pub          # the LXC public key, you need it in step 4
```

## 4. Write both configs

Swap the public keys: each side gets the *other* side's `.pub`. `sudo cat /etc/wireguard/wg1.key`
shows that machine's own private key to paste in.

**VPS**, `/etc/wireguard/wg1.conf` (`sudo nano`, then `sudo chmod 600` it):

```ini
[Interface]
Address = 10.99.0.1/24
ListenPort = 51821
PrivateKey = <VPS private key>

[Peer]
# Docker host running Homelab Manager
PublicKey = <LXC public key>
AllowedIPs = 10.99.0.2/32
```

**Docker host (LXC)**, `/etc/wireguard/wg1.conf` (`chmod 600` it):

```ini
[Interface]
Address = 10.99.0.2/24
PrivateKey = <LXC private key>

[Peer]
# VPS
PublicKey = <VPS public key>
Endpoint = <VPS_IP>:51821
AllowedIPs = 10.99.0.1/32
PersistentKeepalive = 25
```

`AllowedIPs` is deliberately the single peer address on both sides, so no default route changes and
none of the rest of either machine's traffic goes through the tunnel. `PersistentKeepalive` keeps
the home NAT mapping open.

## 5. VPS: open the UDP port

WireGuard drops anything that isn't from a known key without answering, so the port can be open to
the internet without being scannable. If your home IP is static you may restrict the source to it.
Do both of the following, the host firewall and the provider firewall, if you have both.

**Host firewall, ufw (the Ubuntu default):**

```bash
sudo ufw allow 51821/udp comment 'homelab wireguard'
sudo ufw status numbered
```

**Host firewall, plain nftables** (the policy is `drop` on `input`; adjust the table and chain names
to your `/etc/nftables.conf`):

```bash
sudo nft add rule inet filter input udp dport 51821 accept
```

then put the same line in `/etc/nftables.conf` so it survives a reboot.

**Host firewall, plain iptables:**

```bash
sudo iptables -I INPUT -p udp --dport 51821 -j ACCEPT
sudo apt install -y iptables-persistent && sudo netfilter-persistent save
```

**Provider (cloud) firewall**, if the VPS has one in the provider's dashboard: add an *inbound*
rule, protocol UDP, port 51821, source anywhere (or your home IP). You do **not** add a rule for SSH:
the SSH traffic is carried inside this UDP port, so the provider never sees it as port 22.

This does not affect Pangolin: its ports (80/443 TCP, 51820 UDP, and so on) and Docker's own rules
are separate. The new rule only adds one more allowed port.

## 6. VPS: allow SSH only from your PC and the tunnel

Your current rule for your PC stays exactly as it is. Add one more, scoped to the tunnel interface
and to the one address on it:

```bash
sudo ufw allow in on wg1 from 10.99.0.2 to any port 22 proto tcp comment 'homelab dashboard'
sudo ufw status numbered
```

Confirm the list still has your `<PC_IP>` rule for port 22, and nothing else allows port 22. If the
rule for your PC is missing from `ufw status`, stop and fix that first. For raw nftables or iptables
use `iifname "wg1" ip saddr 10.99.0.2 tcp dport 22 accept` or
`iptables -I INPUT -i wg1 -s 10.99.0.2 -p tcp --dport 22 -j ACCEPT`.

If your hardened `sshd` uses `AllowUsers` or `AllowGroups`, it must also let in the dashboard user
(step 8), and you can pin that user to the tunnel address. Check with `sudo sshd -T | grep -iE 'allowusers|allowgroups|listenaddress|passwordauth'`.
If `AllowUsers` exists, extend it (in `/etc/ssh/sshd_config` or a file in `/etc/ssh/sshd_config.d/`):

```
AllowUsers <you> homelab@10.99.0.2
```

Then `sudo sshd -t && sudo systemctl reload ssh`. Do not close your open session; open a new one to
your PC login first to prove you can still get in. `PasswordAuthentication no` should stay as it is.
The dashboard only ever uses its key.

## 7. Bring the tunnel up and make it persistent

On the VPS, then in the LXC:

```bash
sudo wg-quick up wg1
sudo systemctl enable wg-quick@wg1     # start at boot (as root in the LXC, drop sudo)
```

Check on each side:

```bash
sudo wg show wg1        # the VPS shows the peer with a recent "latest handshake" once the LXC pinged
ping -c 3 10.99.0.1     # from the LXC
```

The handshake appears only after traffic flows from the LXC, so ping first. If there is no
handshake, see Troubleshooting.

## 8. VPS: the dashboard's user and permissions

A separate non-root user, so the dashboard never uses your login. Run on the VPS:

```bash
sudo adduser --disabled-password --gecos "" homelab
echo 'homelab ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/homelab-manager
sudo chmod 440 /etc/sudoers.d/homelab-manager && sudo visudo -c
```

Why full `NOPASSWD`: the app runs `sudo -n sh -c '<script>'` (see `sudo: true` in
`homelab/executors.py`), so a rule limited to `apt` commands won't match. The key is therefore
root-equivalent on the VPS, which is exactly why steps 5 to 6 and the next block limit where it can be used.

Install the dashboard's public key (the one from `<PROJECT>/config/id_ed25519.pub`; see "Giving the
dashboard its own SSH key" in the README), locked to the tunnel address and with no extras:

```bash
sudo install -d -m 700 -o homelab -g homelab /home/homelab/.ssh
sudo sh -c 'echo "from=\"10.99.0.2\",restrict $(cat /path/to/id_ed25519.pub)" > /home/homelab/.ssh/authorized_keys'
sudo chown homelab:homelab /home/homelab/.ssh/authorized_keys && sudo chmod 600 /home/homelab/.ssh/authorized_keys
```

Paste the public key line in place of `$(cat ...)` if the file isn't on the VPS: the resulting line
reads `from="10.99.0.2",restrict ssh-ed25519 AAAA... homelab-manager`. `restrict` turns off
forwarding, X11 and the pty, and the app needs none of them.

Connections from the dashboard container leave the LXC through Docker's NAT, so they arrive at the
VPS with the LXC's tunnel address, `10.99.0.2`, which is what `from=` checks. That holds as long as
the compose file uses Docker's default bridge network (it does).

## 9. Point the dashboard at the tunnel

In `<PROJECT>/config/config.yaml`:

```yaml
hosts:
  - { name: vps, host: 10.99.0.1, port: 22, user: homelab, key_file: /config/id_ed25519, sudo: true }
```

Pin the host key over the tunnel (it is authenticated by WireGuard, so this is safer than
scanning the public IP), and compare the fingerprint with the VPS's own:

```bash
cd <PROJECT>/config
ssh-keyscan 10.99.0.1 >> known_hosts
ssh-keygen -lf known_hosts | grep 10.99.0.1
# on the VPS:  ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
sudo chown 10001:10001 known_hosts
```

Drop any old line for the VPS's public IP from `known_hosts`, and redeploy or restart the project in
Arcane so it reads the new config.

## 10. Checks

From the LXC, as the dashboard would connect:

```bash
ssh -i <PROJECT>/config/id_ed25519 -o IdentitiesOnly=yes homelab@10.99.0.1 'sudo -n true && echo ok'
```

It should print `ok`. Then:

- In the dashboard, press Check on the `vps` entry.
- From the LXC, try the VPS's public address: `ssh homelab@<VPS_IP>` must time out. That shows the
  public SSH port is still closed to everything but your PC.
- On the VPS, `sudo journalctl -u ssh -n 20` should show `Accepted publickey for homelab from 10.99.0.2`.
- Reboot the LXC and confirm `sudo wg show wg1` on the VPS gets a fresh handshake without you
  doing anything.
- Pangolin is untouched: its sites and resources should still load.

## Rollback

Reverse order, each step is independent, with your PC session still open:

```bash
# LXC
wg-quick down wg1 && systemctl disable wg-quick@wg1
# VPS
sudo wg-quick down wg1 && sudo systemctl disable wg-quick@wg1
sudo ufw delete allow 51821/udp
sudo ufw status numbered      # delete the "in on wg1 ... port 22" rule by its number: sudo ufw delete <n>
sudo deluser --remove-home homelab && sudo rm /etc/sudoers.d/homelab-manager
```

Also remove `homelab@10.99.0.2` from `AllowUsers` (if you added it), delete the provider firewall
rule for UDP 51821, and remove the `vps` entry from `config.yaml` (or turn the host off in the
dashboard). Your PC's SSH access and Pangolin are never changed by any step, so nothing else needs
restoring.

## Troubleshooting

- **No handshake.** On the VPS `sudo tcpdump -ni any udp port 51821` while the LXC pings. If nothing
  arrives, the provider firewall or `ufw` is blocking UDP 51821, or the endpoint IP in the LXC config
  is wrong. If packets arrive but there is no handshake, a public key is swapped or mistyped
  (each side needs the *other's* public key).
- **`RTNETLINK ... Operation not supported` / `/dev/net/tun` missing in the LXC.** Redo step 2, or
  install `wireguard-go`.
- **Handshake works, ping fails.** Wrong `AllowedIPs` on one side: the VPS peer must be `10.99.0.2/32`,
  the LXC peer `10.99.0.1/32`.
- **SSH: `Permission denied (publickey)`.** The `from=` source isn't `10.99.0.2`, the key line is
  malformed, `AllowUsers` excludes `homelab`, or the home directory permissions are too loose
  (`/home/homelab` must not be group- or world-writable; `.ssh` 700, `authorized_keys` 600).
- **SSH: timeout over the tunnel.** The `ufw ... in on wg1` rule is missing, or you used `wg0` or
  the wrong address.
- **Dashboard shows a known_hosts error for the VPS.** The entry must be for `10.99.0.1`, the host
  the config now uses.
- **A different source address shows in `journalctl`.** The dashboard container isn't going through
  the LXC's NAT (custom network mode). Use `from="<that address>"` or leave the default bridge.
