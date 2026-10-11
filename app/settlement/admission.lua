-- Per-account daily cap: window reset, cap check and increment in one step.
-- Runs only after the pool low-water check has passed; a rejection writes
-- nothing.
--
-- KEYS[1] = pgw:user:{account_id}:used
-- KEYS[2] = pgw:user:{account_id}:used_reset_at
-- ARGV[1] = now_unix
-- ARGV[2] = window_start_unix (the current UTC day)
-- ARGV[3] = next_reset_unix
-- ARGV[4] = cap ("-1" = uncapped)
-- Returns: { admitted, new_used }

local used = tonumber(redis.call('GET', KEYS[1])) or 0
local ws = tonumber(redis.call('GET', KEYS[2])) or tonumber(ARGV[2])
local window_start = tonumber(ARGV[2])
local next_reset = tonumber(ARGV[3])
local now = tonumber(ARGV[1])
local cap = tonumber(ARGV[4])

if ws < window_start then          -- window crossed a reset boundary
  used = 0
  ws = window_start
end

if cap >= 0 and used >= cap then   -- over cap: reject, write nothing
  return {0, used}
end

used = used + 1
local ttl = math.max(1, math.floor(next_reset - now) + 60)  -- + small grace
redis.call('SET', KEYS[1], used, 'EX', ttl)
redis.call('SET', KEYS[2], ws, 'EX', ttl)

return {1, used}
