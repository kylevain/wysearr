# frozen_string_literal: true

# Idempotent in-container Shelfarr convergence for the WyseARR evaluation.
# Secrets arrive through docker-exec stdin and the single result is captured by
# the host bootstrap process. Docker exec streams are not container logs.

require "json"
require "securerandom"

result_sentinel = "WYSEARR_BOOTSTRAP_RESULT="
input = JSON.parse(STDIN.read)
usenet_enabled = input.fetch("usenet_enabled") == true
puid = Integer(ENV.fetch("PUID", "1000"), 10)
pgid = Integer(ENV.fetch("PGID", "1000"), 10)

%w[.secret_key_base .encryption_keys].each do |filename|
  path = File.join("/rails/storage", filename)
  next unless File.file?(path)

  File.chown(puid, pgid, path)
  File.chmod(0o600, path)
end

settings = {
  indexer_provider: "prowlarr",
  indexer_search_scope: "broad",
  prowlarr_url: "http://prowlarr:9696",
  prowlarr_api_key: input.fetch("prowlarr_api_key"),
  # An empty search filter lets Shelfarr query both its isolated Newznab and
  # the existing torrent fallback set. Prowlarr application tags, not this
  # search filter, keep the book indexer out of the ARRs.
  prowlarr_tags: "",
  preferred_download_types: usenet_enabled ? %w[direct usenet torrent] : %w[direct torrent],
  download_local_path: "/downloads",
  download_remote_path: "",
  ebook_output_path: "/ebooks",
  audiobook_output_path: "/audiobooks",
  completed_download_import_mode: "copy",
  remove_completed_usenet_downloads: true,
  immediate_search_enabled: true,
  auto_approve_requests: true,
  auto_select_enabled: true,
  auto_select_confidence_threshold: 90,
  auto_select_min_seeders: 1,
  default_language: "en",
  enabled_languages: ["en"],
  auth_disabled: false,
  # Direct audiobook publication requires its private staging tree and final
  # directory to share a filesystem for atomic rename. The DAS is CIFS, so
  # LibriVox is disabled rather than weakening that safety contract.
  librivox_enabled: false,
  gutenberg_enabled: true,
  anna_archive_enabled: false,
  zlibrary_enabled: false,
  ebooks_com_enabled: false,
  discord_enabled: false,
  discord_webhook_url: "",
  webhook_enabled: false,
  webhook_url: "",
  telegram_enabled: false
}.freeze

settings.each { |key, value| SettingsService.set(key, value) }

admin = User.active.find_or_initialize_by(username: input.fetch("admin_username"))
if admin.new_record?
  admin.name = "WyseARR operator"
  admin.password = input.fetch("admin_password")
  admin.role = :admin
  admin.save!
elsif !admin.admin?
  admin.update!(role: :admin)
end
admin.update!(password: input.fetch("admin_password"))

huey = User.active.find_or_initialize_by(username: "huey")
if huey.new_record?
  huey.name = "Huey automation"
  huey.password = "#{SecureRandom.base58(40)}Aa1"
  huey.role = :user
  huey.save!
elsif !huey.user?
  huey.update!(role: :user)
end

required_scopes = %w[search:read requests:read requests:write].freeze
raw_token = input["existing_huey_token"].to_s
token_record = raw_token.present? ? APIToken.authenticate(raw_token) : nil
token_valid = token_record&.user_id == huey.id && token_record.scope_list.sort == required_scopes.sort

unless token_valid
  huey.api_tokens.active.where(name: "WyseARR Huey").find_each(&:revoke!)
  token_record, raw_token = APIToken.issue!(
    name: "WyseARR Huey",
    user: huey,
    scopes: required_scopes
  )
end

client_attributes = [
  {
    name: "WyseARR qBittorrent",
    client_type: "qbittorrent",
    url: "http://qbittorrent:8080",
    username: input.fetch("qbittorrent_username"),
    password: input.fetch("qbittorrent_password"),
    api_key: nil,
    category: "shelfarr",
    download_path: "/downloads/shelfarr",
    priority: 0,
    enabled: true
  },
  {
    name: "WyseARR SABnzbd",
    client_type: "sabnzbd",
    url: "http://sabnzbd:8080",
    username: nil,
    password: nil,
    api_key: input.fetch("sabnzbd_api_key"),
    category: "shelfarr",
    download_path: "/downloads/usenet",
    priority: 0,
    enabled: usenet_enabled
  }
].freeze

clients = client_attributes.map do |attributes|
  client = DownloadClient.find_or_initialize_by(name: attributes.fetch(:name))
  client.assign_attributes(attributes)
  client.save!
  if client.enabled? && !client.test_connection
    raise "#{client.name} connection validation failed"
  end

  client
end

DownloadMonitorJob.ensure_running!

result = {
  "huey_token" => raw_token,
  "huey_token_reused" => token_valid,
  "settings_count" => settings.length,
  "download_clients" => clients.map(&:name),
  "admin_username" => admin.username
}
STDOUT.write(result_sentinel + JSON.generate(result) + "\n")
STDOUT.flush
