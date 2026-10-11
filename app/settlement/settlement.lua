-- Settlement in one atomic step: idempotency check, window reset, cap guard,
-- counter increment and request event write.
--
-- KEYS[1] = pgw:pool:key:{key_id}:used         (may be absent)
-- KEYS[2] = pgw:pool:key:{key_id}:used_reset_at
-- KEYS[3] = pgw:ops:request:{request_id}       (hash; one field "event")
-- KEYS[4] = pgw:ops:settled:{request_id}       (idempotency marker)
-- ARGV[1] = now_unix
-- ARGV[2] = window_start_unix
-- ARGV[3] = next_reset_unix
-- ARGV[4] = daily_cap ("-1" = uncapped)
-- ARGV[5] = delta (int >= 0; "0" when no key served the request)
-- ARGV[6] = event JSON if the counter moves
-- ARGV[7] = event_ttl_s
-- ARGV[8] = settle_id_ttl_s
-- ARGV[9] = event JSON if it does not
-- Returns: { moved, new_used }
--   moved ==  1 -> counter incremented, new_used is the new value
--   moved ==  0 -> settled without counter movement, new_used == -1
--   moved == -1 -> duplicate; nothing written

if redis.call('EXISTS', KEYS[4]) == 1 then
  return {-1, -1}
end
redis.call('SET', KEYS[4], 1, 'EX', ARGV[8])

local moved = 0
local new_used = -1
local delta = tonumber(ARGV[5])

if delta > 0 then
  local cap = tonumber(ARGV[4])
  local used = tonumber(redis.call('GET', KEYS[1])) or 0
  local ws = tonumber(redis.call('GET', KEYS[2])) or tonumber(ARGV[2])
  local window_start = tonumber(ARGV[2])
  local next_reset = tonumber(ARGV[3])
  local now = tonumber(ARGV[1])

  if ws < window_start then          -- window crossed a reset boundary
    used = 0
    ws = window_start
  end

  if cap < 0 or used < cap then      -- never exceed daily_cap
    used = used + delta
    local ttl = math.max(1, math.floor(next_reset - now) + 60)  -- + small grace
    redis.call('SET', KEYS[1], used, 'EX', ttl)
    redis.call('SET', KEYS[2], ws, 'EX', ttl)
    moved = 1
    new_used = used
  end
end

if moved == 1 then
  redis.call('HSET', KEYS[3], 'event', ARGV[6])
else
  redis.call('HSET', KEYS[3], 'event', ARGV[9])
end
redis.call('EXPIRE', KEYS[3], ARGV[7])

return {moved, new_used}
