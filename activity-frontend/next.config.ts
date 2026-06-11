import type {NextConfig} from "next";

const buildStamp = new Date().toISOString().slice(0, 16);

const nextConfig: NextConfig = {
    output: "export",
    distDir: "out",
    reactCompiler: true,
    assetPrefix: `/v-${buildStamp.replace(/[-:T]/g, "")}`,
    env: {
        NEXT_PUBLIC_BUILD_ID: buildStamp.replace("T", " "),
    },
    images: {
        unoptimized: true,
    },
};

export default nextConfig;
