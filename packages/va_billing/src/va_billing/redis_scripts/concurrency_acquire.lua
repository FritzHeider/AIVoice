-- concurrency_acquire.lua (specs/24 §6 item 6): KEYS[1] sorted set (member -> expiry unix ms);
-- ARGV member, cap (-1 = no cap), now_ms, expiry_ms. Returns 1 when the member holds a slot, else 0.
local now = tonumber(ARGV[3])
local expiry = tonumber(ARGV[4])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  redis.call('ZADD', KEYS[1], 'XX', expiry, ARGV[1])
else
  local cap = tonumber(ARGV[2])
  if cap >= 0 and redis.call('ZCARD', KEYS[1]) >= cap then
    return 0
  end
  redis.call('ZADD', KEYS[1], expiry, ARGV[1])
end
local ttl = redis.call('PTTL', KEYS[1])
if expiry > now and ttl < expiry - now then
  redis.call('PEXPIRE', KEYS[1], expiry - now)
end
return 1
