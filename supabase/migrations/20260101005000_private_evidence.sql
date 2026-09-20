-- Evidence can contain wallet histories. No public bucket or object policy.
INSERT INTO storage.buckets(id,name,public,file_size_limit) VALUES('funded-reward-evidence','funded-reward-evidence',false,67108864)
ON CONFLICT(id) DO UPDATE SET public=false,file_size_limit=67108864;
