# WyseARR role

WyseARR is the home lab's media acquisition and automation node. It owns
qBittorrent, Prowlarr, Sonarr, Radarr, Lidarr, Bazarr, Whisparr, Huey Discord
intake, direct-media importing, local torrent retention, and request state.

It does not own the permanent library or playback applications. Kavita,
Audiobookshelf, RomM, and the DAS live on the Pi-SSD system. WyseARR accesses
their media share only through the host's `/mnt/media` CIFS mount.
