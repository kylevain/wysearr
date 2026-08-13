# frozen_string_literal: true

# Load Shelfarr's persisted Rails keys without evaluating app-writable shell
# syntax, then replace this process with the requested trusted image command.

storage = "/rails/storage"
secret_path = File.join(storage, ".secret_key_base")
keys_path = File.join(storage, ".encryption_keys")
allowed = %w[
  ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY
  ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY
  ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT
].freeze
value_pattern = /\A[A-Za-z0-9_+\/=.-]{16,256}\z/

secret = File.read(secret_path, encoding: "UTF-8").strip
raise "invalid Shelfarr secret key" unless value_pattern.match?(secret)

parsed = {}
File.readlines(keys_path, chomp: true, encoding: "UTF-8").each do |line|
  match = /\Aexport ([A-Z0-9_]+)=(?:"([^"]+)"|'([^']+)'|([^\s]+))\z/.match(line)
  raise "invalid Shelfarr encryption key file" unless match

  name = match[1]
  value = match[2] || match[3] || match[4]
  raise "unexpected Shelfarr encryption key" unless allowed.include?(name)
  raise "duplicate Shelfarr encryption key" if parsed.key?(name)
  raise "invalid Shelfarr encryption key value" unless value_pattern.match?(value)

  parsed[name] = value
end
raise "incomplete Shelfarr encryption key file" unless parsed.keys.sort == allowed.sort
raise "missing trusted command" if ARGV.empty?

ENV["SECRET_KEY_BASE"] = secret
parsed.each { |name, value| ENV[name] = value }
exec(*ARGV)
