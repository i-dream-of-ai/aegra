FROM node:20-alpine

# Install Docker CLI for running LEAN containers via mounted socket
# Install Python for algorithm validation (AST parsing)
RUN apk add --no-cache docker-cli python3

WORKDIR /app

# Copy package files
COPY package.json ./

# Install all dependencies with npm (simpler than pnpm in Docker)
RUN npm install

# Copy source
COPY . .

# Build TypeScript
RUN npm run build

# Expose port
EXPOSE 3001

# Start
CMD ["node", "dist/index.js"]
