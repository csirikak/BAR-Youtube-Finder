# Add the System.Drawing assembly to get image properties
Add-Type -AssemblyName System.Drawing

Get-ChildItem -Path . -Recurse -Filter "*.png" | ForEach-Object {
    try {
        $img = [System.Drawing.Image]::FromFile($_.FullName)
        $w = $img.Width
        $h = $img.Height
        $img.Dispose() # Must release the file lock

        if ($w -lt 1280 -or $h -lt 720) {
            Write-Host "Deleting: $($_.FullName) ($w x $h)"
            # This line performs the actual deletion
            Remove-Item -Path $_.FullName -Force 
        }
    } catch {
        Write-Warning "Could not process file: $($_.FullName) - $_"
    }
}